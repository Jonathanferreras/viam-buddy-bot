"""End of a voice shell game: lift the cup the player picked so everyone can see what's under it,
put it back down, and return to the overview pose.

Env: REVEAL_RUN (the round's folder, with execution.json), REVEAL_SLOT (left/middle/right),
REVEAL_HOLD_S (seconds to hold it up, default 4). Uses the cup positions the round planned (where it
set each cup down) and the same collision-checked straight moves as round.py.
"""
import asyncio, contextlib, json, os
from pathlib import Path

import numpy as np

from game_lib import FIELDS, GRIP_Z, OVERVIEW, RETREAT_Z, Rig, connect, run, stage

LIFT_MM = 90.                 # how high the cup comes up (its rim clears the table by about this much)
TRAVEL_Z = 300.               # gripper-frame height for moving across the cups
SPEED = (40., 80.)


def log(msg):
    print(msg, flush=True)


async def main():
    folder = Path(os.environ['REVEAL_RUN'])
    slot = os.environ['REVEAL_SLOT']
    hold_s = float(os.environ.get('REVEAL_HOLD_S', '4'))
    report = json.loads((folder / 'execution.json').read_text())
    if not report.get('completed'):
        raise RuntimeError('That round didn’t finish, so the cup positions aren’t known')
    occupied = report.get('occupied') or report.get('private_final_occupied') or {}
    slots = {k: np.array(v[:2]) for k, v in report['slots'].items()}
    if slot not in occupied or slot not in slots:
        raise RuntimeError(f'There’s no cup at {slot}')
    bias = np.array(report['placement_compensation_xy_mm'])
    cups = {k: slots[k] for k in occupied}                 # every cup where the round left it
    others = {k: v for k, v in cups.items() if k != slot}
    tcp = slots[slot] + bias

    robot = await connect()
    async with robot:
        rig = Rig(robot, log)
        try:
            if await rig.arm.is_moving() or await rig.held():
                raise RuntimeError('Arm 18 must be still with an empty gripper')
            here = await rig.pose()
            rig.orientation = {k: getattr(here, k) for k in FIELDS}
            await rig.speed(*SPEED)
            stage('reveal', f'Lifting the {slot} cup')
            await rig.rise(TRAVEL_Z, cups)          # the shuffle ends just above the last cup it moved
            await rig.go(tcp, TRAVEL_Z, False, f'over the {slot} cup', cups)
            await rig.go(tcp, RETREAT_Z, False, f'approach the {slot} cup', others)
            await rig.go(tcp, GRIP_Z, False, f'reach the {slot} cup', others)
            await rig.jaw(False)
            await rig.go(tcp, GRIP_Z + LIFT_MM, True, f'lift the {slot} cup', others)
            stage('lifted', f'Here’s the {slot} cup')
            await asyncio.sleep(hold_s)
            stage('putback', f'Putting the {slot} cup back')
            await rig.go(tcp, GRIP_Z, True, f'lower the {slot} cup', others)
            await rig.jaw(True)
            await rig.go(tcp, RETREAT_Z, False, f'let go of the {slot} cup', others)
            await rig.to_overview(cups)
        except BaseException:
            with contextlib.suppress(Exception):
                await rig.arm.stop(timeout=3)
            raise


if __name__ == '__main__':
    run(main)
