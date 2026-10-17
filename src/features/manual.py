"""Manual mode: put an arm in hand-guided ("zero gravity") mode from its card on the status page, or by voice,
so you can move it by hand — to teach its starting position or the clean-up spot — then lock it again.

The xArm driver does the work: DoCommand {"enter_manual_mode": true} / {"exit_manual_mode": true}. While an
arm is in manual mode its controller refuses motion commands by itself ("arm is in manual mode"), so nothing
else can drive it while you're holding it. Leaving manual mode doesn't move it: the servos take over and
hold it wherever you left it. "Stop" also ends manual mode (the driver restarts the arm), and so does a
timeout, in case it's forgotten.

Voice:  "manual mode" / "let me move you" / "free drive"   -> on   (an arm number picks the arm: "manual mode 18")
        "manual mode off" / "done with manual" / "lock"... -> off
Page:   POST /action {"action": "manual_mode", "arm": "<resource>", "on": true | false}
        POST /action {"action": "manual_save_home", "arm": "<resource>"}  -> where it is now becomes its start
DoCommand: {"manual_mode": {"arm": "arm17-arm", "on": true}}  (or {"manual_mode": "status"})
Optional config, in the router's attributes:
    "manual": {"auto_off_min": 10}     # leave manual mode by itself after this long (0 = never)
"""
import asyncio
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from . import Feature

_OFF = {"off", "exit", "done", "lock", "leave", "end", "finish", "finished", "disable", "out", "quit"}


def _m(router):
    """The router's own module (main runs as __main__, so it's looked up rather than imported)."""
    return sys.modules[type(router).__module__]


def _on(router) -> Dict[str, Dict[str, Any]]:
    if getattr(router, "_manual", None) is None:
        router._manual = {}
    return router._manual


def _auto_off_s(router) -> float:
    return max(0.0, float((getattr(router, "_manual_cfg", None) or {}).get("auto_off_min", 10))) * 60.0


def _arm(router, key: str = "") -> Tuple[str, Dict[str, Any]]:
    """(resource name, {name, arm}) for `key` (a resource or a display name); default: the picker, else the first arm."""
    arms = getattr(router, "_arms", {}) or {}
    if key:
        if key in arms:
            return key, arms[key]
        for rn, info in arms.items():
            if info.get("name") == key:
                return rn, info
        raise ValueError(f"there's no arm called '{key}'")
    rn = getattr(router, "picker", None) if getattr(router, "picker", None) in arms else next(iter(arms), None)
    if rn is None:
        raise ValueError("there's no arm to put in manual mode")
    return rn, arms[rn]


def _refresh(router) -> None:
    """Forget arms a "Stop" has taken out of manual mode since they went in (Stop restarts the arm)."""
    on = _on(router)
    if not on:
        return
    stops = [h["time"] for h in list(getattr(router, "_history", []) or [])
             if h.get("stop") or str(h.get("label") or "").startswith("Stopped")]
    for rn, st in list(on.items()):
        if any(t > st["since"] for t in stops):
            on.pop(rn, None)
            _cancel_timer(router, rn)
            M = _m(router)
            M.LOGGER.info(f"{M._cap((getattr(router, '_arms', {}).get(rn) or {}).get('name', rn))}: "
                          "out of manual mode (stopped)")


def _cancel_timer(router, rn: str) -> None:
    t = (getattr(router, "_manual_timers", None) or {}).pop(rn, None)
    if t is not None and not t.done() and t is not asyncio.current_task():
        t.cancel()


async def _auto_off(router, rn: str, since: float, after: float) -> None:
    await asyncio.sleep(after)
    st = _on(router).get(rn)
    if st is None or st["since"] != since:
        return
    M = _m(router)
    name = (getattr(router, "_arms", {}).get(rn) or {}).get("name", rn)
    try:
        await _set(router, rn, False)
        M.LOGGER.info(f"{M._cap(name)}: left manual mode after {after / 60:.0f} minutes")
    except Exception as e:
        M.LOGGER.warning(f"{M._cap(name)}: couldn't leave manual mode by itself: {M._friendly_error(e)}")


async def _set(router, rn: str, on: bool) -> Dict[str, Any]:
    """Put arm `rn` in (or take it out of) manual mode."""
    M = _m(router)
    _refresh(router)
    info = (getattr(router, "_arms", {}) or {}).get(rn) or {}
    arm, name = info.get("arm"), info.get("name", rn)
    if arm is None:
        raise ValueError(f"{name} isn't available (is its machine online?)")
    if on:
        task = getattr(router, "_action_task", None)
        if task is not None and not task.done() and rn in (getattr(router, "_action_arms", None) or []) \
                and task is not asyncio.current_task():
            raise ValueError(f"{name} is busy; wait for it to finish (or stop it) first")
        if await asyncio.wait_for(arm.is_moving(), timeout=5):
            raise ValueError(f"{name} is moving; wait for it to stop first")
        g = (getattr(router, "grabbers", {}) or {}).get(rn)
        await asyncio.wait_for(arm.do_command({"enter_manual_mode": True}), timeout=15)
        since = time.time()
        _on(router)[rn] = {"since": since}
        _cancel_timer(router, rn)
        after = _auto_off_s(router)
        if after > 0:
            timers = router.__dict__.setdefault("_manual_timers", {})
            timers[rn] = asyncio.get_event_loop().create_task(_auto_off(router, rn, since, after))
        M.LOGGER.info(f"{M._cap(name)}: manual mode on — it can be moved by hand"
                      + (f" (holding the {g['holding']}: hold it steady)" if g and g.get("holding") else ""))
    else:
        await asyncio.wait_for(arm.do_command({"exit_manual_mode": True}), timeout=15)
        _on(router).pop(rn, None)
        _cancel_timer(router, rn)
        M.LOGGER.info(f"{M._cap(name)}: manual mode off — holding where it is")
    return {"arm": name, "resource": rn, "manual": on}


# ---------------------------------------------------------------- speech

def understand(router, words: List[str], text: str) -> Optional[Dict[str, Any]]:
    w = set(words)
    t = " " + " ".join(words) + " "
    manual = "manual" in w or "freedrive" in w or " free drive " in t or " let me move " in t
    if not manual or not getattr(router, "_arms", None):
        return None
    nums = [x for x in words if x.isdigit()]
    rn = router._arm_by_number(nums[0]) if nums else None
    try:
        rn, info = _arm(router, rn or "")
    except ValueError:
        return None
    off = bool(w & _OFF)
    return router._mk_cmd(action="manual_off" if off else "manual_on", resource_name=rn, resource=info.get("arm"),
                          name=info.get("name", rn),
                          label=f"Manual mode {'off' if off else 'on'} for {info.get('name', rn)}")


async def run(router, cmd: Dict[str, Any], text: str) -> Dict[str, Any]:
    M = _m(router)
    on = cmd["action"] == "manual_on"
    res = await _set(router, cmd["resource_name"], on)
    name = M._cap(res["arm"])
    router._current = (f"{name} is in manual mode — move it by hand, then say “manual mode off”" if on
                       else f"{name} is out of manual mode and holding still")
    cmd["keep_current"] = True
    return res


def arms_for(router, cmd: Dict[str, Any]) -> List[str]:
    return []          # nothing is driven: the arm only changes mode


def label(cmd: Dict[str, Any]) -> str:
    return "Manual mode on" if cmd["action"] == "manual_on" else "Manual mode off"


# ---------------------------------------------------------------- page, DoCommand, state

async def _page_set(router, body):
    rn, _ = _arm(router, str(body.get("arm") or ""))
    return await _set(router, rn, bool(body.get("on", True)))


async def _page_save_home(router, body):
    rn, _ = _arm(router, str(body.get("arm") or ""))
    return await router._save_home(rn)


async def _do(router, command):
    v = command.get("manual_mode")
    if isinstance(v, dict):
        rn, _ = _arm(router, str(v.get("arm") or ""))
        return await _set(router, rn, bool(v.get("on", True)))
    return state(router)


def configure(router, attrs: Dict[str, Any]) -> None:
    router._manual_cfg = dict(attrs.get("manual") or {})
    _on(router)


def state(router) -> Dict[str, Any]:
    _refresh(router)
    after = _auto_off_s(router)
    return {"arms": {rn: {"since": st["since"], "auto_off_at": st["since"] + after if after else None}
                     for rn, st in _on(router).items()},
            "auto_off_min": after / 60.0}


FEATURE = Feature(
    name="manual",
    actions=("manual_on", "manual_off"),
    words=("manual mode", "manual mode off", "let me move you", "free drive", "done exit lock finish"),
    understand=understand,
    run=run,
    arms_for=arms_for,
    label=label,
    configure=configure,
    page_actions={"manual_mode": _page_set, "manual_save_home": _page_save_home},
    do_commands={"manual_mode": _do},
    state=state,
)
