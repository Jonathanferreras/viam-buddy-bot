"""Closing the gripper gently, the way arm 18 grips the cups in the shell game (shell_game/robot/game_lib.py
`jaw`): a torque-limited close at a moderate jaw speed that stops as soon as the jaws stall on the thing,
then a short wait until the jaw position holds still before deciding whether it's held.

UFactory xArm gripper units: position 0 = shut, 850 = wide open (~100 mm, ~8.5 units per mm); torque is
the closing force (the shell game uses 10); jaw speed up to ~5000 (the shell game uses 500).
"""
import asyncio
from typing import Any, Dict, Optional

# the shell game's numbers (shell_game/robot/fast-shuffle-tuning.json + game_lib.jaw)
TORQUE = 10.0
JAW_SPEED = 500.0
STALL_S = 0.2
SETTLE_TIMEOUT_S = 4.0


async def gentle_close(gripper, torque: float = TORQUE, jaw_speed: float = JAW_SPEED, stall_s: float = STALL_S,
                       target: float = 0.0, arm=None) -> Optional[bool]:
    """Close on whatever is between the fingers. True/False = held or not; None = this gripper doesn't take
    a torque-limited close (the caller falls back to its normal grab)."""
    cmd = {"grab_with_torque": {"position": float(target), "speed": float(jaw_speed), "torque": float(torque),
                                "stall_seconds": float(stall_s)}}
    sent = False
    for res in (gripper, arm):            # the gripper takes it; the shell game sends it to the arm
        if res is None:
            continue
        try:
            await res.do_command(cmd, timeout=10)
            sent = True
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
    if not sent:
        return None
    return await _settled_hold(gripper, target)


async def _settled_hold(gripper, target: float) -> bool:
    """Wait until the jaws stop moving (3 readings within 2 units), then say whether something is held."""
    loop = asyncio.get_event_loop()
    t0, samples, held = loop.time(), [], False
    while loop.time() - t0 < SETTLE_TIMEOUT_S:
        hs = await gripper.is_holding_something(timeout=3)
        held = bool(getattr(hs, "is_holding_something", False))
        try:
            pos = float((await gripper.do_command({"get": True}, timeout=3)).get("pos", -1))
        except asyncio.CancelledError:
            raise
        except Exception:
            return held                   # no position readout: go by is_holding_something alone
        if pos >= 0 and pos <= target + 3:
            return False                  # closed all the way: nothing between the fingers
        samples = (samples + [pos])[-3:]
        if len(samples) == 3 and max(samples) - min(samples) <= 2:
            return held
        await asyncio.sleep(0.15)
    return held

