"""After a stop: if arm 18 is holding a cup, lower it straight down onto the table, let go, and go
back to the overview pose. If it isn't holding anything, just go back to the overview pose.

The other cups (from the latest scan) are obstacles, except one right below the gripper.
"""
import contextlib, math

import numpy as np

from game_lib import FIELDS, GRIP_Z, RETREAT_Z, Rig, connect, read_scan, run, stage


def log(msg):
    print(msg, flush=True)


async def main():
    try:
        slots, _ = read_scan(max_age_s=24 * 3600)
    except Exception:
        slots = {}
    robot = await connect()
    async with robot:
        rig = Rig(robot, log)
        try:
            if await rig.arm.is_moving():
                await rig.arm.stop(timeout=3)
            here = await rig.pose()
            rig.orientation = {k: getattr(here, k) for k in FIELDS}
            await rig.speed(30., 60.)
            clear = {k: v for k, v in slots.items() if math.dist((here.x, here.y), tuple(v)) > 60}
            if await rig.held():
                stage('putdown', 'Putting the cup down')
                if here.z > GRIP_Z + 1:
                    await rig.go((here.x, here.y), GRIP_Z, True, 'lower the cup', clear)
                await rig.jaw(True)
                await rig.go((here.x, here.y), RETREAT_Z, False, 'let go of the cup', clear)
                slots = {**clear, 'held': np.array([here.x, here.y])}
            stage('putdown', 'Going back to the start')
            await rig.to_overview(slots)
        except BaseException:
            with contextlib.suppress(Exception):
                await rig.arm.stop(timeout=3)
            raise


if __name__ == '__main__':
    run(main)
