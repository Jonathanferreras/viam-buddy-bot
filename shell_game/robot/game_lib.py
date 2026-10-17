"""Shared pieces for the voice-driven shell game (setup_ball.py, reveal_cup.py).

The moves and gripper checks follow round.py: every move is a collision-checked straight line from
the builtin motion service, keeping the overview orientation, with the other cups as obstacles, and
the gripper's hold is confirmed before and watched during each move.
"""
import asyncio, json, math, os, sys, time, traceback
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
# Import every resource type before connecting: the SDK only makes clients, at connect time, for
# the resource types already imported. (Importing Arm after connecting -> "No arm named 'arm'".)
from viam.robot.client import RobotClient
from viam.components.arm import Arm
from viam.components.camera import Camera
from viam.components.gripper import Gripper
from viam.services.motion import MotionClient

ROOT = Path(__file__).resolve().parent
FIELDS = ('x', 'y', 'z', 'o_x', 'o_y', 'o_z', 'theta')
OVERVIEW = (200., -560., 350.)
GRIP_Z, RETREAT_Z = 100., 130.          # gripper-frame heights round.py grips and retreats at


def stage(sid, text):
    """One line the voice router turns into the game's stage on the status page."""
    print(f'STAGE {sid} {text}', flush=True)


def tuning():
    t = json.loads((ROOT / 'fast-shuffle-tuning.json').read_text())
    return {'jaw_speed': float(t['jaw_speed']), 'open_position': float(t['open_position']),
            'stall_seconds': float(t.get('stall_seconds', .2))}


def placement_bias():
    """Where the gripper must be relative to a cup's center (round.py's measured compensation)."""
    b = json.loads((ROOT / 'middle-left-swap-02/parking-reacquisition.json').read_text())['mean_center_world_mm']
    a = json.loads((ROOT / 'middle-left-swap-03/middle-reacquisition.json').read_text())['mean_center_world_mm']
    oldb = json.loads((ROOT / 'middle-left-swap-02/execution.json').read_text())
    olda = json.loads((ROOT / 'middle-left-swap-03/execution.json').read_text())
    pb = next(e['actual'] for e in oldb['events'] if e['event'] == 'move_completed' and e['phase'] == 'slide')
    pa = next(e['actual'] for e in olda['events'] if e['event'] == 'move_completed' and e['phase'] == 'slide')
    biases = [np.array([p[k] - c[i] for i, k in enumerate(('x', 'y'))]) for p, c in ((pb, b), (pa, a))]
    if np.linalg.norm(biases[0] - biases[1]) > 10:
        raise RuntimeError('Placement offsets disagree')
    bias = np.mean(biases, axis=0)
    if np.linalg.norm(bias) > 30:
        raise RuntimeError('Placement compensation outside bound')
    return bias


def read_scan(max_age_s=300):
    """The three cups from the latest stereo scan, named left/middle/right as the camera sees them,
    plus the table plane (world z = a*x + b*y + c)."""
    scanfile = ROOT / 'stereo-scan/automatic-analysis.json'
    if time.time() - scanfile.stat().st_mtime > max_age_s:
        raise RuntimeError('Fresh layout required')
    scan = json.loads(scanfile.read_text())
    meta = json.loads((ROOT / 'stereo-scan/capture.json').read_text())
    R = np.array(meta['basis_world_columns']).T
    origin = np.array([meta['camera_world'][k] for k in ('x', 'y', 'z')])
    cups = [c['cap_fit'] for c in scan['object_clusters'] if c['points'] > 1500 and c.get('cap_fit')
            and 20 < c['cap_fit']['radius_mm'] < 40 and c['cap_fit']['boundary_rms_mm'] < 3]
    if len(cups) != 3:
        raise RuntimeError(f'I need to see exactly three cups (found {len(cups)})')

    def pixel_x(c):
        p = R.T @ (np.array(c['center_world_mm']) - origin)
        return p[0] / p[2]
    cups.sort(key=pixel_x)
    slots = {n: np.array(c['center_world_mm'][:2]) for n, c in zip(('left', 'middle', 'right'), cups)}
    return slots, np.array(scan['table_plane_world_z_eq_ax_by_c'])


def connect():
    load_dotenv(ROOT / '.env')
    return asyncio.wait_for(RobotClient.at_address(
        os.environ['VIAM_MACHINE_ADDRESS'],
        RobotClient.Options.with_api_key(api_key=os.environ['VIAM_API_KEY'], api_key_id=os.environ['VIAM_API_KEY_ID'])), 30)


class Rig:
    """Arm 18, its gripper and the motion service, moving the way round.py does."""

    def __init__(self, robot, log):
        self.robot, self.log = robot, log
        self.arm = Arm.from_robot(robot, 'arm')
        self.grip = Gripper.from_robot(robot, 'gripper')
        self.motion = MotionClient.from_robot(robot, 'builtin')
        self.t = tuning()
        self.orientation = None
        self.last_hold = None

    async def pose(self, frame='gripper'):
        from viam.proto.common import Pose, PoseInFrame
        return (await self.robot.transform_pose(PoseInFrame(reference_frame=frame, pose=Pose(o_z=1)), 'world')).pose

    async def held(self):
        return (await self.grip.is_holding_something(timeout=3)).is_holding_something

    async def speed(self, deg_s, deg_s2):
        await self.arm.do_command({'set_speed': float(deg_s), 'set_acceleration': float(deg_s2)}, timeout=5)

    @staticmethod
    def world(stationary):
        """The table margin and every cup that isn't being moved, as boxes (round.py's sizes)."""
        from viam.proto.common import Pose, WorldState, GeometriesInFrame, Geometry, RectangularPrism, Vector3
        geoms = [Geometry(center=Pose(x=200, y=-550, z=-10, o_z=1), box=RectangularPrism(dims_mm=Vector3(x=650, y=600, z=50)),
                          label='playing-surface-margin')]
        for name, (x, y) in stationary.items():
            geoms.append(Geometry(center=Pose(x=float(x), y=float(y), z=65, o_z=1),
                                  box=RectangularPrism(dims_mm=Vector3(x=110, y=110, z=150)), label='stationary-cup-' + name))
        return WorldState(obstacles=[GeometriesInFrame(reference_frame='world', geometries=geoms)])

    async def go(self, xy, z, holding, phase, stationary):
        """A straight, collision-checked move of the gripper to (x, y, z), stopping if the grip changes."""
        from viam.proto.common import Pose, PoseInFrame
        from viam.proto.service.motion import Constraints, LinearConstraint
        if await self.held() != holding:
            raise RuntimeError('Grip state mismatch before ' + phase)
        target = Pose(**{**self.orientation, 'x': float(xy[0]), 'y': float(xy[1]), 'z': float(z)})
        self.log(f'move {phase} -> {float(xy[0]):.0f},{float(xy[1]):.0f},{float(z):.0f}')
        task = asyncio.create_task(self.motion.move(
            component_name='gripper', destination=PoseInFrame(reference_frame='world', pose=target),
            world_state=self.world(stationary),
            constraints=Constraints(linear_constraint=[LinearConstraint(line_tolerance_mm=.2, orientation_tolerance_degs=.5)]),
            timeout=50))
        try:
            async with asyncio.timeout(55):
                while not task.done():
                    done, _ = await asyncio.wait([task], timeout=.25)
                    if done:
                        break
                    if await self.held() != holding:
                        await self.arm.stop(timeout=3)
                        raise RuntimeError('Grip state changed during ' + phase)
                if not await task:
                    raise RuntimeError('Motion planner returned false for ' + phase)
        finally:
            if not task.done():
                task.cancel()
                await self.arm.stop(timeout=3)

    async def jaw(self, opening):
        """Close on a cup (torque-limited) or open to the release width, then wait for the jaws to settle."""
        if await self.arm.is_moving():
            raise RuntimeError('Arm moving before jaw action')
        pos_target = self.t['open_position'] if opening else 450.
        await self.arm.do_command({'grab_with_torque': {'position': pos_target, 'speed': self.t['jaw_speed'], 'torque': 10.,
                                                        'stall_seconds': self.t['stall_seconds']}}, timeout=10)
        samples = []
        for _ in range(80):
            state = await self.grip.is_holding_something(timeout=3)
            pos = (await self.grip.do_command({'get': True}, timeout=3)).get('pos', 0)
            status = int((state.meta or {}).get('status', 1)) & 3
            ok = (not state.is_holding_something and status == 0 and pos >= pos_target - 10) if opening \
                else (state.is_holding_something and status == 2 and 450 < pos < 800)
            samples = (samples + [pos])[-3:] if ok else []
            if len(samples) == 3 and max(samples) - min(samples) <= 2:
                if not opening:
                    self.last_hold = float(np.mean(samples))
                return
            await asyncio.sleep(.15)
        raise RuntimeError('The gripper didn’t ' + ('let go' if opening else 'get a firm hold of the cup'))

    async def rise(self, z, stationary, phase='rise'):
        """Straight up to height z (empty gripper). The cup right below, if any, isn't an obstacle for
        this: the gripper starts beside it, and going straight up only moves away from it."""
        here = await self.pose()
        if here.z < z - 1:
            clear = {k: v for k, v in stationary.items() if math.dist((here.x, here.y), tuple(v)) > 60}
            await self.go((here.x, here.y), z, False, phase, clear)

    async def to_overview(self, stationary):
        """Up to the overview height where it is, then across to the overview pose (empty gripper)."""
        await self.rise(OVERVIEW[2], stationary)
        await self.go(OVERVIEW[:2], OVERVIEW[2], False, 'overview', stationary)


def run(main):
    """Run a script's main(); on any failure or Ctrl+C ask the arm to stop and exit non-zero."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('STOPPED: interrupted', flush=True)
        sys.exit(130)
    except Exception as e:
        traceback.print_exc(file=sys.stdout)       # the whole story goes in the run's log
        why = getattr(e, 'message', None) or str(e) or type(e).__name__
        print(f'STOPPED: {why}', flush=True)
        sys.exit(1)
