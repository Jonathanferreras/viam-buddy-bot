"""Arm 18 sets up a Jenga tower from blocks lying anywhere on its mat.

  build_tower.py [--layers 6]

Each round: look from the overview pose (one 3D scan from the wrist camera), pick the loose block
nearest the tower that the fingers can reach, grip it by its two ends, and set it in the next slot:
3 blocks per layer, each layer turned 90 degrees. After every block it looks again (the tower must
be as tall as it should be). The first grab of a run pauses a few cm above the block so you can check
the aim. Close to the table the arm moves in straight lines worked out here (src/kin.py, the same
xArm 850 kinematics the buddy bot uses), because the motion planner won't let the fingers that low;
the way down is slow and watched: if a joint feels a push it shouldn't, the arm stops.

Prints "STAGE <id> <text>" and "PROGRESS <placed> <total>" lines for the voice router. Tuning is in
jenga-tuning.json; the tower spot and progress are kept in tower.json so a stopped build can resume.
"""
import argparse, asyncio, contextlib, json, math, os, sys, time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT / 'shell_game' / 'robot'))
sys.path.insert(0, str(PROJECT / 'shell_game'))
sys.path.insert(0, str(PROJECT / 'src'))
import game_lib                                     # noqa: E402  (imports the SDK resource types before connecting)
from game_lib import Camera, Rig, connect, stage    # noqa: E402
import kin                                          # noqa: E402
import blocks as B                                  # noqa: E402

from viam.components.arm import JointPositions      # noqa: E402
from viam.proto.component.arm import MoveOptions    # noqa: E402
from viam.proto.common import Pose, PoseInFrame     # noqa: E402

TUNING = json.loads((HERE / 'jenga-tuning.json').read_text())
STATE_FILE = HERE / 'tower.json'


def log(msg):
    print(msg, flush=True)


def progress(placed, total):
    print(f'PROGRESS {placed} {total}', flush=True)


class Stopped(Exception):
    pass


class Builder:
    def __init__(self, robot, rig, t, layers):
        self.robot, self.rig, self.arm, self.grip = robot, rig, rig.arm, rig.grip
        self.t, self.layers, self.total = t, layers, layers * 3
        self.block = t['block']
        self.cam = Camera.from_robot(robot, 'cam')
        bias = t.get('camera_bias_xy_mm')
        self.bias = np.array(bias, float) if bias is not None else game_lib.placement_bias()
        self.tcp = float(t['finger_mm'])
        self.rehearsed = not t.get('rehearse_first_pick', True)
        self.q_home = None
        self.bad = []           # (x, y) of blocks that couldn't be reached this run

    # ---------------- moving ----------------
    async def joints(self):
        return [float(v) for v in (await self.arm.get_joint_positions(timeout=5)).values]

    async def follow(self, path, speed, watch=False):
        if not path:
            return
        opts = MoveOptions(max_vel_degs_per_sec=float(speed), max_acc_degs_per_sec2=float(self.t['accel_deg_s2']))
        task = asyncio.create_task(self.arm.move_through_joint_positions([JointPositions(values=q) for q in path],
                                                                         options=opts, timeout=60))
        base = None
        try:
            if watch:
                base = np.array((await self.arm.do_command({'load': ''}, timeout=3))['load'][:6], float)
            while not task.done():
                await asyncio.wait([task], timeout=.12)
                if watch and not task.done():
                    await self.check_load(base)
            await task
            if watch:            # and once more at the bottom, before the gripper does anything
                await self.check_load(base)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(Exception):
                    await self.arm.stop(timeout=3)

    def route(self, q, T, via_home=True):
        """Joint paths (one per leg) from joints q to flange pose T, all worked out before anything moves:
        a straight line if there is one, else via the overview position at this height, else via the
        overview pose itself (from the edge of the reach the arm may have to flip a joint on the way)."""
        p = kin.line(q, T)
        if p is not None or not via_home or self.q_home is None:
            return None if p is None else [p]
        H = kin.fk(self.q_home)
        mid = kin.fk(q).copy()
        mid[:3, 3] = (H[0, 3], H[1, 3], max(mid[2, 3], T[2, 3]))
        for legs in ([mid, T], [H, T], [mid, H, T]):
            paths, cur = [], q
            for leg in legs:
                lp = kin.line(cur, leg)
                if lp is None:
                    break
                paths.append(lp)
                cur = lp[-1]
            else:
                return paths
        return None

    async def check_load(self, base):
        load = np.array((await self.arm.do_command({'load': ''}, timeout=3))['load'][:6], float)
        if np.max(np.abs(load[1:4] - base[1:4])) > self.t['load_limit_nm']:
            await self.arm.stop(timeout=3)
            log(f'joint loads {np.round(load, 1).tolist()} vs {np.round(base, 1).tolist()} before the move')
            raise Stopped('I felt something push back on the way down, so I stopped')

    async def line_to(self, T, speed, watch=False, via_home=True):
        q = await self.joints()
        paths = self.route(q, T, via_home=via_home and not watch)
        if paths is None:
            now = kin.fk(q)
            log(f'no route from {np.round(now[:3, 3]).tolist()} (x axis {np.round(now[:3, 0], 2).tolist()}) '
                f'to {np.round(T[:3, 3]).tolist()} (x axis {np.round(T[:3, 0], 2).tolist()}); joints {np.round(q, 1).tolist()}')
            raise Stopped('I can’t reach there in a straight line')
        for p in paths:
            await self.follow(p, speed, watch)

    def down_pose(self, xy, flange_z, axis_perp, q_ref):
        """Flange pointing down over (x, y) with its x axis along ±axis_perp (fingers close along its y);
        of the two, the one that turns the wrist least from q_ref."""
        best = None
        seeds = [q_ref]
        if self.q_home is not None:      # the solver is local: also start from the overview pose, turned toward the spot
            h = list(self.q_home)
            here, there = kin.fk(h)[:2, 3], np.array(xy, float)
            turn = math.degrees(math.atan2(there[1], there[0]) - math.atan2(here[1], here[0]))
            seeds += [h, [h[0] + turn] + h[1:]]
        for s in (1, -1):
            T = kin.pose_down(xy[0], xy[1], flange_z, (s * axis_perp[0], s * axis_perp[1]))
            for seed in seeds:
                q = kin.ik(T, seed)
                if q is not None:
                    if best is None or abs(q[5] - q_ref[5]) < best[0]:
                        best = (abs(q[5] - q_ref[5]), T)
                    break
        if best is None:
            raise Stopped('That spot is out of reach')
        return best[1]

    async def home(self):
        await self.line_to(kin.fk(self.q_home), self.t['travel_speed_deg_s'])

    # ---------------- gripper ----------------
    async def jaw(self, close):
        pos_target = self.t['close_position'] if close else self.t['open_position']
        await self.arm.do_command({'grab_with_torque': {'position': float(pos_target), 'speed': float(self.t['grip_speed']),
                                                        'torque': float(self.t['grip_torque']), 'stall_seconds': .2}}, timeout=10)
        samples = []
        for _ in range(60):
            state = await self.grip.is_holding_something(timeout=3)
            pos = (await self.grip.do_command({'get': True}, timeout=3)).get('pos', 0)
            status = int((state.meta or {}).get('status', 1)) & 3
            ok = (state.is_holding_something and status == 2 and 450 < pos < 800) if close \
                else (not state.is_holding_something and pos >= pos_target - 15)
            samples = (samples + [pos])[-3:] if ok else []
            if len(samples) == 3 and max(samples) - min(samples) <= 2:
                return float(np.mean(samples))
            await asyncio.sleep(.12)
        if close:
            raise Stopped('The gripper didn’t get a hold of the block')
        raise Stopped('The gripper didn’t open')

    # ---------------- looking ----------------
    async def scan(self, tower_xy):
        cloud, _ = await self.cam.get_point_cloud(timeout=20)
        o = await self.rig.pose('cam')
        origin = np.array([o.x, o.y, o.z])
        cols = []
        for xyz in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
            b = (await self.robot.transform_pose(PoseInFrame(reference_frame='cam', pose=Pose(x=xyz[0], y=xyz[1], z=xyz[2], o_z=1)), 'world')).pose
            cols.append([b.x - o.x, b.y - o.y, b.z - o.z])
        R = np.array(cols).T
        off = cloud.index(b'DATA binary\n') + len(b'DATA binary\n')
        a = np.frombuffer(cloud, offset=off, dtype=[('x', '<f4'), ('y', '<f4'), ('z', '<f4'), ('rgb', '<u4')])
        xyz = np.column_stack([a['x'], a['y'], a['z']]).astype(float) * 1000
        ok = np.isfinite(xyz).all(1) & (xyz[:, 2] > 200) & (xyz[:, 2] < 1500)
        world = xyz[ok] @ R.T + origin
        rgb = a['rgb'][ok]
        keep = (world[:, 0] > -150) & (world[:, 0] < 700) & (world[:, 1] > -900) & (world[:, 1] < -200) & (world[:, 2] > -80)
        return B.find(world[keep], rgb[keep], self.block, tower_xy=tower_xy)

    # ---------------- one block ----------------
    async def pick(self, b, plane):
        """Grip block b by its ends and lift it to travel height. Returns the fingertip height above
        the block's bottom while holding it."""
        x, y = np.array(b['center']) + self.bias
        axis = np.array(b['axis'])
        perp = (-axis[1], axis[0])                     # flange x across the block: fingers close along its length
        table = plane[0] * x + plane[1] * y + plane[2]
        grip_tip = table + self.t['grip_above_table_mm']
        travel = table + self.t['travel_above_table_mm'] + self.tcp
        q = await self.joints()
        T_travel = self.down_pose((x, y), travel, perp, q)
        await self.line_to(T_travel, self.t['travel_speed_deg_s'])
        xa = T_travel[:3, 0]
        await self.jaw(close=False)
        if not self.rehearsed:
            hover = table + b['height'] + self.t['rehearse_hover_mm'] + self.tcp
            await self.line_to(kin.pose_down(x, y, hover, xa), self.t['slow_speed_deg_s'] * 2)
            stage('aim', 'Checking my aim: my fingers should be just past both ends of the block. Say “stop” if not.')
            await asyncio.sleep(self.t['rehearse_pause_s'])
            self.rehearsed = True
            stage('pick', 'Picking up a block')
        await self.line_to(kin.pose_down(x, y, grip_tip + self.t['approach_above_mm'] + self.tcp, xa), self.t['travel_speed_deg_s'])
        await self.line_to(kin.pose_down(x, y, grip_tip + self.tcp, xa), self.t['slow_speed_deg_s'], watch=True)
        held = await self.jaw(close=True)
        log(f'gripped at {x:.0f},{y:.0f} (jaw {held:.0f})')
        await self.line_to(T_travel, self.t['slow_speed_deg_s'] * 2)
        if not (await self.grip.is_holding_something(timeout=3)).is_holding_something:
            raise Stopped('I dropped the block on the way up')
        return grip_tip - table

    async def place(self, slot, layer, tower_xy, plane, tip_above_bottom):
        x, y = np.array(slot['center']) + self.bias
        axis = np.array(slot['axis'])
        perp = (-axis[1], axis[0])
        table = plane[0] * tower_xy[0] + plane[1] * tower_xy[1] + plane[2]
        bottom = table + layer * self.block['height_mm'] + self.t['drop_mm']
        travel = table + self.t['travel_above_table_mm'] + self.tcp
        q = await self.joints()
        T_travel = self.down_pose((x, y), travel, perp, q)
        await self.line_to(T_travel, self.t['travel_speed_deg_s'])
        xa = T_travel[:3, 0]
        tip = bottom + tip_above_bottom
        await self.line_to(kin.pose_down(x, y, tip + self.t['approach_above_mm'] + self.tcp, xa), self.t['travel_speed_deg_s'])
        await self.line_to(kin.pose_down(x, y, tip + self.tcp, xa), self.t['slow_speed_deg_s'], watch=True)
        await self.jaw(close=False)
        await self.line_to(T_travel, self.t['slow_speed_deg_s'] * 2)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2) + '\n')


async def main(args):
    t = TUNING
    layers = args.layers or t['layers']
    total = layers * 3
    stage('ready', 'Getting ready')
    from run import prepare                   # shell_game/run.py: open the gripper, go to the overview pose
    await prepare(True)
    robot = await connect()
    async with robot:
        rig = Rig(robot, log)
        bld = Builder(robot, rig, t, layers)
        try:
            if await rig.arm.is_moving():
                raise Stopped('Arm 18 is moving')
            if await rig.held():
                raise Stopped('Arm 18 is holding something; put it down first')
            q = await bld.joints()
            here = await rig.arm.get_end_position(timeout=5)
            if math.dist(kin.fk(q)[:3, 3], (here.x, here.y, here.z)) > 3:
                raise Stopped('My arm model doesn’t match arm 18 here, so I won’t move it in straight lines')
            bld.q_home = q                                  # the overview pose (play.py put the arm there)
            stage('scan', 'Looking for blocks')
            st = load_state()
            tower_xy = st.get('tower_xy')
            scene = await bld.scan(tower_xy)
            placed = 0
            if tower_xy and scene['tower'] and st.get('placed'):
                want = math.ceil(st['placed'] / 3)
                if abs(scene['tower']['layers'] - want) <= 0 and st['placed'] < total:
                    placed = int(st['placed'])
                    log(f'resuming: {placed} blocks already in the tower')
            if not placed:
                spot = B.tower_spot({**scene, 'others': scene['others'] + ([scene['tower']] if scene['tower'] else [])},
                                    bld.block, t['tower_area_xy'], t['tower_prefer_xy'], need_r=t['tower_clear_mm'])
                if spot is None:
                    raise Stopped('There’s no clear spot for the tower: clear a patch about 20 cm across in front of arm 18')
                tower_xy = list(spot)
                scene = await bld.scan(tower_xy)
            save_state({'tower_xy': tower_xy, 'placed': placed, 'layers': layers, 'time': time.time()})
            progress(placed, total)
            log(f'tower at {tower_xy[0]:.0f},{tower_xy[1]:.0f}')
            waited, last_wait = 0.0, None
            while placed < total:
                layer, k = divmod(placed, 3)
                choices = [b for b in B.pick_order(scene, bld.block, tower_xy)
                           if not any(math.dist(b['center'], p) < 15 for p in bld.bad)]
                if not choices:
                    edge = sum(1 for o in scene['others'] if o['kind'] == 'block_on_edge')
                    far = sum(1 for b in scene['blocks'] if any(math.dist(b['center'], p) < 15 for p in bld.bad))
                    need = total - placed
                    msg = (f'I need {need} more block{"s" if need > 1 else ""}: lay {"them" if need > 1 else "it"} flat on the mat, a little apart'
                           + (f'. {edge} {"is" if edge == 1 else "are"} standing on edge' if edge else '')
                           + (f'. {far} {"is" if far == 1 else "are"} out of my reach: move {"it" if far == 1 else "them"} closer' if far else ''))
                    if msg != last_wait:
                        stage('wait', msg)
                        last_wait = msg
                    if waited >= t['wait_for_blocks_s']:
                        raise Stopped(msg)
                    await asyncio.sleep(5)
                    waited += 5
                    scene = await bld.scan(tower_xy)
                    continue
                waited, last_wait = 0.0, None
                b = choices[0]
                stage('pick', f'Layer {layer + 1} of {layers}: picking up a block')
                try:
                    tip = await bld.pick(b, scene['plane'])
                except Stopped as e:
                    if 'reach' in str(e):
                        bld.bad.append(b['center'])
                        await bld.home()
                        scene = await bld.scan(tower_xy)
                        continue
                    raise
                slot = B.layer_slots(tower_xy, layer, bld.block, t['pitch_mm'], t['base_axis'])[k]
                stage('place', f'Layer {layer + 1} of {layers}: placing block {k + 1} of 3')
                await bld.place(slot, layer, tower_xy, scene['plane'], tip)
                placed += 1
                save_state({'tower_xy': tower_xy, 'placed': placed, 'layers': layers, 'time': time.time()})
                progress(placed, total)
                await bld.home()
                stage('check', 'Checking the tower')
                scene = await bld.scan(tower_xy)
                want_h = math.ceil(placed / 3) * bld.block['height_mm']
                tw = scene['tower']
                if tw is None or abs(tw['height'] - want_h) > 7:
                    got = 'nothing' if tw is None else f'{tw["height"]:.0f} mm'
                    raise Stopped(f'The tower doesn’t look right (expected about {want_h:.0f} mm tall, saw {got}). '
                                  'Fix it or clear it, then start again')
            stage('done', f'Tower built: {layers} layers, {total} blocks. Your move!')
            save_state({'tower_xy': tower_xy, 'placed': placed, 'layers': layers, 'time': time.time(), 'done': True})
        except Stopped as e:
            print(f'STOPPED: {e}', flush=True)
            sys.exit(1)
        except BaseException:
            with contextlib.suppress(Exception):
                await rig.arm.stop(timeout=3)
            raise


if __name__ == '__main__':
    import fcntl
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--layers', type=int, choices=range(1, 19), default=None)
    args = ap.parse_args()
    lock = open(PROJECT / 'shell_game' / 'robot' / '.run.lock', 'w')     # one game drives arm 18 at a time
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('STOPPED: another game is using arm 18', flush=True)
        sys.exit(1)
    game_lib.run(lambda: main(args))
