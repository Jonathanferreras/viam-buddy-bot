""""Hey buddy, dance": both arms wiggle together for a few seconds, then go back to exactly where they were.

The dance is small moves around each arm's current pose: the base sways, the forearm rolls, the wrist nods
and twists — arm 18 mirrors arm 17. Before anything moves, every pose of the dance is checked with the
arm's own kinematics (src/kin.py):
  * every joint stays inside its limits,
  * the fingertips stay at least `clearance_mm` above the table, never rise more than `max_rise_mm`,
    and never wander more than `max_reach_mm` sideways from where they started.
If a pose breaks a rule, the whole dance is scaled down (to 75 %, 50 %, 30 %) until it fits. If our
kinematics don't match where the arm says it is, only the two moves that can't change the fingertips'
height are used (base sway and wrist twist). An arm that's holding something, in manual mode, or too low
to dance safely sits this one out (it's said why). "Stop" halts both where they are, like any command.

Optional config, in the router's attributes:
    "dance": {"beats": 12, "speed_deg_s": 120, "accel_deg_s2": 500,
              "clearance_mm": 100, "max_reach_mm": 130, "max_rise_mm": 120, "scale": 1.0}
"""
import asyncio
import math
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import Feature
from .. import kin

_WORDS = {"dance", "dancing", "boogie", "groove", "party", "wiggle"}
# one bar of the dance, in degrees from the start pose: [J1 sway, J2, J3, J4 roll, J5 nod, J6 twist]
_STEPS = [
    [8, 0, 0, 15, 10, 35],
    [-8, 0, 0, -15, -10, -35],
    [8, 0, 0, -15, 10, 35],
    [-8, 0, 0, 15, -10, -35],
    [0, 0, 0, 20, 0, 60],
    [0, 0, 0, -20, 0, -60],
]


def _m(router):
    return sys.modules[type(router).__module__]


def _cfg(router) -> Dict[str, Any]:
    return getattr(router, "_dance_cfg", None) or {}


def understand(router, words: List[str], text: str) -> Optional[Dict[str, Any]]:
    if not (set(words) & _WORDS) or not getattr(router, "_arms", None):
        return None
    return router._mk_cmd(action="dance", resource_name="both arms", name="the arms", label="Dancing")


def _tip(q: List[float], finger: float) -> Tuple[float, float, float]:
    T = kin.fk(q)
    p = T[:3, 3] + T[:3, 2] * finger
    return float(p[0]), float(p[1]), float(p[2])


def _plan(q0: List[float], mirror: bool, ok_kin: bool, finger: float, table_z: float, cfg) -> Tuple[Optional[List[List[float]]], str]:
    """The dance's joint positions for one arm, scaled down until they all fit; (None, why) if nothing fits."""
    clear, reach, rise = float(cfg.get("clearance_mm", 100)), float(cfg.get("max_reach_mm", 130)), float(cfg.get("max_rise_mm", 120))
    beats = max(2, min(40, int(cfg.get("beats", 12))))
    x0, y0, z0 = _tip(q0, finger)
    if ok_kin and z0 < table_z + clear:
        return None, f"its fingers are only {z0 - table_z:.0f} mm above the table — too low to dance"
    sign = -1.0 if mirror else 1.0
    base = float(cfg.get("scale", 1.0))
    for s in (1.0, 0.75, 0.5, 0.3):
        s *= base
        poses, bad = [], ""
        for b in range(beats):
            d = _STEPS[b % len(_STEPS)]
            q = [q0[j] + sign * s * d[j] for j in range(6)]
            if not ok_kin:                       # unknown geometry: only moves that keep the fingertips' height
                q = [q0[0] + sign * s * d[0], q0[1], q0[2], q0[3], q0[4], q0[5] + sign * s * d[5]]
            for j, (lo, hi) in enumerate(kin.LIMITS):
                if not lo + 3 <= q[j] <= hi - 3:
                    bad = f"joint {j + 1} would pass its limit"
            if ok_kin and not bad:
                x, y, z = _tip(q, finger)
                if z < table_z + clear:
                    bad = "the fingers would come too close to the table"
                elif z > z0 + rise:
                    bad = "it would reach up too far"
                elif math.hypot(x - x0, y - y0) > reach:
                    bad = "it would swing out too far"
            if bad:
                break
            poses.append(q)
        if not bad:
            return poses, ("" if s >= base else f"a smaller dance ({round(s * 100)} %) to stay in its space")
    return None, f"there's no room to dance where it is ({bad})"


async def _kin_ok(arm, q0: List[float]) -> bool:
    try:
        here = await arm.get_end_position()
        T = kin.fk(q0)
        return math.dist(T[:3, 3], (here.x, here.y, here.z)) <= 5
    except Exception:
        return False


async def run(router, cmd: Dict[str, Any], text: str) -> Dict[str, Any]:
    M = _m(router)
    cfg = _cfg(router)
    v = getattr(router, "vision", None) or {}
    finger, table_z = float(v.get("finger_mm", 172)), float(v.get("table_z_mm", -23))
    fast = {"speed_d": float(cfg.get("speed_deg_s", 120)), "acceleration_d": float(cfg.get("accel_deg_s2", 500))}
    manual = getattr(router, "_manual", None) or {}
    grabbers = getattr(router, "grabbers", {}) or {}
    plans, sitting = [], []
    for i, (rn, info) in enumerate(router._arms.items()):
        arm, name = info.get("arm"), M._cap(info.get("name") or rn)
        if arm is None:
            sitting.append(f"{name} isn't connected"); continue
        if rn in manual:
            sitting.append(f"{name} is in manual mode"); continue
        g = grabbers.get(rn)
        if g is not None and g.get("holding"):
            sitting.append(f"{name} is holding the {g['holding']}"); continue
        q0 = [float(x) for x in (await arm.get_joint_positions()).values]
        if len(q0) != 6:
            sitting.append(f"{name} isn't a 6-joint arm"); continue
        ok = await _kin_ok(arm, q0)
        poses, why = _plan(q0, mirror=bool(i % 2), ok_kin=ok, finger=finger, table_z=table_z, cfg=cfg)
        if poses is None:
            sitting.append(f"{name}: {why}"); continue
        if why:
            M.LOGGER.info(f"dance: {name} does {why}")
        if not ok:
            M.LOGGER.info(f"dance: {name}'s kinematics don't match its position; only swaying and twisting")
        plans.append((name, arm, q0, poses))
    if not plans:
        raise M.NotFound("Nobody can dance right now: " + "; ".join(sitting))
    router._action_arms = [rn for rn, info in router._arms.items() if any(p[1] is info.get("arm") for p in plans)]
    who = " and ".join(p[0] for p in plans)
    router._set_phase("running", current=f"{who} {'are' if len(plans) > 1 else 'is'} dancing")
    for s in sitting:
        M.LOGGER.info(f"dance: {s}, so it sits this one out")

    async def go(name, arm, q0, poses):
        try:
            for q in poses:
                await arm.move_to_joint_positions(M.JointPositions(values=q), extra=fast)
            await arm.move_to_joint_positions(M.JointPositions(values=q0), extra=fast)
        except asyncio.CancelledError:
            raise                                   # "stop": stay where it halted
        except Exception:
            try:                                    # something failed partway: back to where it started
                await arm.move_to_joint_positions(M.JointPositions(values=q0))
            except Exception as e2:
                M.LOGGER.error(f"dance: {name} couldn't go back to where it started: {e2}")
            raise

    res = await asyncio.gather(*[go(*p) for p in plans], return_exceptions=True)
    for r in res:
        if isinstance(r, asyncio.CancelledError):
            raise r
    errs = [f"{p[0]}: {M._friendly_error(r)}" for p, r in zip(plans, res) if isinstance(r, Exception)]
    if errs:
        raise RuntimeError("; ".join(errs))
    router._current = f"{who} danced" + (f" ({'; '.join(sitting)}, so it sat out)" if sitting else "")
    cmd["keep_current"] = True
    return {"danced": [p[0] for p in plans], "sat_out": sitting}


def configure(router, attrs: Dict[str, Any]) -> None:
    router._dance_cfg = dict(attrs.get("dance") or {})


def arms_for(router, cmd: Dict[str, Any]) -> List[str]:
    return [rn for rn, info in (getattr(router, "_arms", None) or {}).items() if info.get("arm") is not None]


async def _do(router, command):
    return await router._route("dance", typed=True)


FEATURE = Feature(
    name="dance",
    actions=("dance",),
    words=("dance dancing boogie groove party wiggle",),
    configure=configure,
    understand=understand,
    run=run,
    arms_for=arms_for,
    label=lambda cmd: "Dancing",
    do_commands={"dance": _do},
    page_actions={"dance": lambda router, body: _do(router, {})},
)
