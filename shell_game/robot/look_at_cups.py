"""After the shuffle: back to the overview pose, where the wrist camera sees all three cups, so the
player can see them (and tap one) while they guess.

Env: SHUFFLE_RUN (the round's folder, relative to robot/). Writes <run>/guess_view.json: where each
cup sits in the wrist camera's picture, as fractions of its width/height, for the status page to put
tap targets on the live view: {"left": {"u": .., "v": .., "r": ..}, ...} (r: fraction of the width).
"""
import contextlib, json, os

import numpy as np

from game_lib import FIELDS, ROOT, Camera, Rig, connect, run, stage

CUP_TOP_Z = 90.        # the cups' tops sit about here (table at z ~0)
TAP_RADIUS_MM = 45.    # a little wider than a cup


def log(msg):
    print(msg, flush=True)


async def main():
    from viam.proto.common import Pose, PoseInFrame
    folder = ROOT / os.environ['SHUFFLE_RUN']
    report = json.loads((folder / 'execution.json').read_text())
    occupied = report.get('occupied') or report.get('private_final_occupied') or {}
    cups = {k: np.array(report['slots'][k][:2]) for k in occupied}
    robot = await connect()
    async with robot:
        rig = Rig(robot, log)
        try:
            if await rig.arm.is_moving() or await rig.held():
                raise RuntimeError('Arm 18 must be still with an empty gripper')
            here = await rig.pose()
            rig.orientation = {k: getattr(here, k) for k in FIELDS}
            await rig.speed(60., 120.)
            stage('shuffle', 'Getting a good look at the cups')
            await rig.to_overview(cups)

            # where each cup shows up in the wrist camera's picture from here
            camera = Camera.from_robot(robot, 'cam')
            p = (await camera.get_properties(timeout=10)).intrinsic_parameters
            fx, fy, cx, cy = p.focal_x_px, p.focal_y_px, p.center_x_px, p.center_y_px
            W, H = p.width_px or 2 * cx, p.height_px or 2 * cy
            o = await rig.pose('cam')
            origin = np.array([o.x, o.y, o.z])
            cols = []
            for xyz in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
                b = (await robot.transform_pose(PoseInFrame(reference_frame='cam', pose=Pose(x=xyz[0], y=xyz[1], z=xyz[2], o_z=1)), 'world')).pose
                cols.append([b.x - o.x, b.y - o.y, b.z - o.z])
            R = np.array(cols).T
            view = {}
            for name, (x, y) in cups.items():
                pc = R.T @ (np.array([x, y, CUP_TOP_Z]) - origin)
                if pc[2] <= 0:
                    continue
                u, v = fx * pc[0] / pc[2] + cx, fy * pc[1] / pc[2] + cy
                view[name] = {'u': round(float(u / W), 4), 'v': round(float(v / H), 4),
                              'r': round(float(TAP_RADIUS_MM * fx / pc[2] / W), 4)}
            (folder / 'guess_view.json').write_text(json.dumps(view) + '\n')
            log(f'cups in view: {view}')
        except BaseException:
            with contextlib.suppress(Exception):
                await rig.arm.stop(timeout=3)
            raise


if __name__ == '__main__':
    run(main)
