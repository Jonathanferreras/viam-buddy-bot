"""Start of a voice shell game: lift the middle cup, wait for the player to set the red ball down where
it stood, then put the cup back over the ball and return to the overview pose.

Needs: arm at the overview pose with an empty, open gripper (play.py's prepare step) and a fresh scan
(capture_stereo.py + detect_cups_auto.py). Prints STAGE lines for the voice router.

While waiting, the arm holds the cup high (cup mouth ~20 cm above the table) and to one side, with
its wrist camera straight above the empty spot, and watches for a red blob of ball height there.
"Settled" = seen in the same place (within 5 mm) in three frames in a row, with no hand over the spot.
"""
import asyncio, contextlib, json, math, os, time

import numpy as np

from game_lib import FIELDS, GRIP_Z, OVERVIEW, RETREAT_Z, ROOT, Rig, connect, placement_bias, read_scan, run, stage

HOVER_Z = 300.          # gripper-frame height while holding the cup up (stationary cups top out at z 140)
SPOT_MAX_MM = 40.       # the ball must be within this of where the middle cup stood
SETTLE_MM, SETTLE_FRAMES = 5., 3
SPEED = (45., 90.)      # deg/s, deg/s^2 for these moves (slower than the shuffle)


def log(msg):
    print(msg, flush=True)


def decode_depth(data):
    """Viam raw depth (image/vnd.viam.dep): 'DEPTHMAP' + width(u64 BE) + height(u64 BE) + u16 BE mm."""
    if data[:8] != b'DEPTHMAP':
        raise ValueError('depth image is not in Viam raw depth format')
    w, h = int.from_bytes(data[8:16], 'big'), int.from_bytes(data[16:24], 'big')
    return np.frombuffer(data[24:24 + w * h * 2], dtype='>u2').reshape(h, w).astype(np.float32)


def detect_ball(rgb, depth, intr, R, origin, plane, spot, held_xy, roi_mm=150.):
    """Look for the red ball near `spot` (world x, y). rgb: HxWx3 uint8, depth: HxW mm (0 = none),
    R: camera axes as columns in world, origin: camera position in world, plane: table z = a*x+b*y+c.
    Returns {'xy': ball center or None, 'ball_px', 'hand_px', 'off_mm'}."""
    import cv2
    fx, fy, cx, cy = intr
    H, W = depth.shape
    s = np.array([spot[0], spot[1], plane[0] * spot[0] + plane[1] * spot[1] + plane[2]])
    pc = R.T @ (s - origin)
    if pc[2] <= 0:
        return {'xy': None, 'ball_px': 0, 'hand_px': 0, 'off_mm': None, 'why': 'spot behind camera'}
    u0, v0 = fx * pc[0] / pc[2] + cx, fy * pc[1] / pc[2] + cy
    r = roi_mm * fx / pc[2]
    x0, x1 = int(max(0, u0 - r)), int(min(W, u0 + r))
    y0, y1 = int(max(0, v0 - r)), int(min(H, v0 + r))
    if x1 - x0 < 10 or y1 - y0 < 10:
        return {'xy': None, 'ball_px': 0, 'hand_px': 0, 'off_mm': None, 'why': 'spot out of view'}
    z = depth[y0:y1, x0:x1]
    vv, uu = np.mgrid[y0:y1, x0:x1]
    ok = z > 0
    z, uu, vv = z[ok], uu[ok], vv[ok]
    cam = np.stack([(uu - cx) / fx * z, (vv - cy) / fy * z, z], axis=1)
    world = cam @ R.T + origin
    height = world[:, 2] - (plane[0] * world[:, 0] + plane[1] * world[:, 1] + plane[2])
    d_spot = np.hypot(world[:, 0] - spot[0], world[:, 1] - spot[1])
    d_held = np.hypot(world[:, 0] - held_xy[0], world[:, 1] - held_xy[1])
    hsv = cv2.cvtColor(rgb[y0:y1, x0:x1][ok].reshape(-1, 1, 3), cv2.COLOR_RGB2HSV).reshape(-1, 3)
    red = ((hsv[:, 0] <= 10) | (hsv[:, 0] >= 170)) & (hsv[:, 1] >= 110) & (hsv[:, 2] >= 50)
    ball = red & (height > 4) & (height < 70) & (d_spot < 120)
    hand = (height > 70) & (height < 170) & (d_spot < 110) & (d_held > 60)
    # a 30 mm ball seen from here covers about pi*(15*fx/z)^2 pixels; ask for a fraction of that
    need = max(150, int(0.15 * math.pi * (15 * fx / pc[2]) ** 2))
    out = {'ball_px': int(ball.sum()), 'hand_px': int(hand.sum()), 'need_px': need, 'xy': None, 'off_mm': None}
    if out['ball_px'] >= need:
        xy = np.median(world[ball][:, :2], axis=0)
        out['xy'] = xy
        out['off_mm'] = float(np.hypot(*(xy - np.asarray(spot))))
    return out


async def watch_for_ball(rig, robot, spot, held_xy, plane, timeout_s):
    from viam.proto.common import Pose, PoseInFrame
    import cv2
    from game_lib import Camera
    camera = Camera.from_robot(robot, 'cam')
    p = (await camera.get_properties(timeout=10)).intrinsic_parameters
    intr = (p.focal_x_px, p.focal_y_px, p.center_x_px, p.center_y_px)
    o = await rig.pose('cam')
    origin = np.array([o.x, o.y, o.z])
    cols = []
    for xyz in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
        b = (await robot.transform_pose(PoseInFrame(reference_frame='cam', pose=Pose(x=xyz[0], y=xyz[1], z=xyz[2], o_z=1)), 'world')).pose
        cols.append([b.x - o.x, b.y - o.y, b.z - o.z])
    R = np.array(cols).T
    said, recent, t_end = None, [], time.monotonic() + timeout_s

    def say(msg):
        nonlocal said
        if msg != said:
            stage('ball', msg)
            said = msg
    say('Put the red ball where the middle cup was')
    while time.monotonic() < t_end:
        frames, _ = await camera.get_images(timeout=10)
        color = depth = None
        for f in frames:
            mime = str(f.mime_type).lower()
            if 'dep' in mime:
                depth = decode_depth(bytes(f.data))
            elif color is None and ('jpeg' in mime or 'png' in mime):
                color = cv2.cvtColor(cv2.imdecode(np.frombuffer(bytes(f.data), np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
        if color is None or depth is None or color.shape[:2] != depth.shape:
            raise RuntimeError('Arm 18’s camera needs aligned color and depth images')
        res = detect_ball(color, depth, intr, R, origin, plane, spot, held_xy)
        log(f"look: ball {res['ball_px']}/{res.get('need_px')} px, hand {res['hand_px']} px, off {res['off_mm']}")
        if res['hand_px'] > 400:
            recent = []
            say('Waiting for your hand to move away')
        elif res['xy'] is None:
            recent = []
            say('Put the red ball where the middle cup was')
        elif res['off_mm'] > SPOT_MAX_MM:
            recent = []
            say(f'Move the ball about {max(1, round((res["off_mm"] - SPOT_MAX_MM / 2) / 10))} cm closer to the middle spot')
        else:
            recent = (recent + [res['xy']])[-SETTLE_FRAMES:]
            spread = max(np.hypot(*(a - b)) for a in recent for b in recent)
            if len(recent) == SETTLE_FRAMES and spread <= SETTLE_MM:
                return np.mean(recent, axis=0)
            say('Got the ball — waiting for it to settle')
        await asyncio.sleep(.3)
    return None


async def main():
    slots, plane = read_scan()
    bias = placement_bias()
    timeout_s = float(os.environ.get('BALL_TIMEOUT', '180'))
    folder = ROOT / os.environ['SHUFFLE_RUN'] if os.environ.get('SHUFFLE_RUN') else None
    robot = await connect()
    async with robot:
        rig = Rig(robot, log)
        try:
            if await rig.arm.is_moving() or await rig.held():
                raise RuntimeError('Arm 18 must be still with an empty gripper')
            here = await rig.pose()
            if math.dist([here.x, here.y, here.z], OVERVIEW) > 1:
                raise RuntimeError('Arm 18 isn’t at the overview pose')
            rig.orientation = {k: getattr(here, k) for k in FIELDS}
            cam = await rig.pose('cam')
            d = np.array([cam.x - here.x, cam.y - here.y])     # wrist camera relative to the gripper, in world
            await rig.speed(*SPEED)
            others = {k: slots[k] for k in ('left', 'right')}
            spot = slots['middle']
            tcp = spot + bias

            stage('lift', 'Lifting the middle cup')
            await rig.go(tcp, RETREAT_Z, False, 'approach middle cup', others)
            await rig.go(tcp, GRIP_Z, False, 'reach middle cup', others)
            await rig.jaw(False)
            await rig.go(tcp, HOVER_Z, True, 'lift middle cup', others)
            hover = spot - d                                   # wrist camera straight above the spot
            await rig.go(hover, HOVER_Z, True, 'look at the spot', others)

            ball = await watch_for_ball(rig, robot, spot, hover - bias, plane, timeout_s)
            target = spot if ball is None else ball
            stage('cover', 'Covering the ball' if ball is not None else 'No ball seen — putting the cup back')
            await rig.go(target + bias, HOVER_Z, True, 'over the ball', others)
            await rig.go(target + bias, GRIP_Z, True, 'lower the cup', others)
            await rig.jaw(True)
            await rig.go(target + bias, RETREAT_Z, False, 'let go of the cup', others)
            await rig.to_overview({**others, 'middle': target})
            if folder is not None:
                (folder / 'ball.json').write_text(json.dumps({'ball_xy': None if ball is None else ball.tolist(),
                                                              'middle_spot_xy': spot.tolist()}) + '\n')
            if ball is None:
                raise RuntimeError(f'No red ball showed up in {round(timeout_s)} s, so the cup went back')
            log(f'ball covered at {ball[0]:.0f},{ball[1]:.0f}')
        except BaseException:
            with contextlib.suppress(Exception):
                await rig.arm.stop(timeout=3)
            raise


if __name__ == '__main__':
    run(main)
