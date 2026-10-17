""""Hey buddy, clean up": the picker arm takes every verified thing on the desk (the ones the camera has
put a name to, i.e. the page's "Verified" group) and sets them down at the side, one by one, then goes
back to its starting position.

Where "the side" is gets taught once: move the arm over the spot and say "this is the clean up spot"
(or press the page's button, or DoCommand {"cleanup_spot": "save"}). It's kept in wave_poses.json. Things
are set down around that spot in a row, `spacing_mm` apart, alternating either side of it (centre, one
side, the other side, ...), along the direction across the arm's reach so every slot stays as far from
the base as the taught one. A slot that something already stands on is skipped, and nothing within half a
spacing of a slot counts as "on the desk", so things already at the side are left alone.

Optional config, in the router's attributes:
    "cleanup": {"arm": "arm17-arm",      # which arm (default: vision.picker)
                "spacing_mm": 80,        # gap between things set down at the side
                "max_items": 8,          # most things moved in one go
                "direction": [0, 1]}     # row direction in the arm's base frame (default: across its reach)

"Stop" halts it like any other command; the arm stays where it halted (still holding, if it was).
"""
import asyncio
import math
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from . import Feature

_GENERIC = re.compile(r"\bobject( \d+)?$", re.I)          # the camera couldn't name it ("White object 2")
_CLEAN = {"clean", "tidy", "clear", "cleanup"}
_WHOLE = {"up", "table", "desk", "everything", "all", "mess"}
_SPOT = {"spot", "here", "area", "place", "side"}
_TEACH = {"this", "save", "remember", "set", "mark"}


def _m(router):
    """The router's own module (main runs as __main__, so it's looked up rather than imported)."""
    return sys.modules[type(router).__module__]


def _cfg(router) -> Dict[str, Any]:
    return getattr(router, "_cleanup_cfg", None) or {}


def _arm(router) -> Optional[Dict[str, Any]]:
    gs = getattr(router, "grabbers", None) or {}
    want = _cfg(router).get("arm")
    if want:
        return gs.get(want) or next((g for g in gs.values() if g["name"] == want), None)
    return gs.get(router.picker) if getattr(router, "picker", None) else next(iter(gs.values()), None)


def _key(g) -> str:
    return "cleanup:" + g["key"]


def _spot(router, g) -> Optional[Dict[str, Any]]:
    return (getattr(router, "_poses", {}) or {}).get(_key(g))


def _verified(o: Dict[str, Any]) -> bool:
    label = o.get("tag") or o.get("label") or ""
    return bool(label) and not _GENERIC.search(label)


def _name(o: Dict[str, Any]) -> str:
    t = o.get("tag") or o.get("label") or "thing"
    return t[:1].lower() + t[1:]


def _where(router, g, o, c) -> Optional[List[float]]:
    """Where `o` (seen by camera `c`) is in arm `g`'s base frame, or None if that can't be worked out."""
    try:
        if c.get("arm"):
            return router._wrist_point(o, c)[0]
        if g["table_link"].ready:
            return router._link_target(g, o, c)[0]
        cam = router._object_cam_mm(o, c)
        if cam is not None and g["calibration"].ready:
            return list(g["calibration"].to_arm(cam))
    except Exception:
        pass
    return None


def _pick_plan(router, g, o, c):
    """(where it is, a function that starts the pick) — the same three ways a clicked grab goes."""
    info = router._object_grab_info(o, c)
    if not info:
        return None, None, "grabbing isn't set up for this camera"
    if "why" in info:
        return None, None, info["why"]
    if info["key"] != g["key"]:
        return None, None, f"it's {info['arm']}'s to reach, not {g['name']}'s"
    what = _name(o)
    if info.get("via") == "wrist":
        p, why = router._wrist_point(o, c)
        h = float((o.get("size_mm") or [0, 0, 0])[2] or 0)
        return p, (lambda: router._pick_at_steps(g, p, what, h, router._wrist_extras(o, c))), why
    if info.get("via") == "link":
        p, extras, why = router._link_target(g, o, c)
        return p, (lambda: router._pick_at_steps(g, p, what, extras["height"], extras)), why
    cam = router._object_cam_mm(o, c)
    return list(g["calibration"].to_arm(cam)), (lambda: router._pick_steps(g, cam, what)), None


def _slots(router, g, spot) -> List[Tuple[float, float]]:
    cfg, v = _cfg(router), router.vision
    gap = float(cfg.get("spacing_mm", 80))
    n = int(cfg.get("max_items", 8))
    x0, y0 = spot["x"], spot["y"]
    d = cfg.get("direction")
    if d:
        dx, dy = float(d[0]), float(d[1])
    else:   # across the arm's reach: every slot about as far from the base as the taught spot
        r = math.hypot(x0, y0) or 1.0
        dx, dy = -y0 / r, x0 / r
    k = math.hypot(dx, dy) or 1.0
    dx, dy = dx / k, dy / k
    out = []
    for i in range(4 * n):
        step = (i + 1) // 2 * (1 if i % 2 else -1)        # 0, +1, -1, +2, -2, ...
        x, y = x0 + step * gap * dx, y0 + step * gap * dy
        if v["reach_min_mm"] + 20 <= math.hypot(x, y) <= v["reach_max_mm"] - 20:
            out.append((x, y))
        if len(out) >= n:
            break
    return out


def _near(p, pts, r) -> bool:
    return any(math.hypot(p[0] - q[0], p[1] - q[1]) < r for q in pts)


# ---------------------------------------------------------------- speech

def understand(router, words: List[str], text: str) -> Optional[Dict[str, Any]]:
    # "cleaning" -> "clean", "tidying" -> "tidy": the stem is what counts
    w = {next((c for c in _CLEAN if x.startswith(c)), x) for x in words}
    if not (w & _CLEAN):
        return None
    g = _arm(router)
    if g is None or not getattr(router, "vision", None):
        return None
    base = dict(resource_name=g["key"], resource=g["arm"], name=g["name"])
    if w & _SPOT and w & _TEACH:          # "this is the clean up spot", "remember this spot for cleaning"
        return router._mk_cmd(action="clean_up_spot", label="Saving the clean-up spot", **base)
    if w & _WHOLE or "cleanup" in words:      # "clean up", "tidy up the desk", "clear the table"
        return router._mk_cmd(action="clean_up", label="Cleaning up", **base)
    return None


# ---------------------------------------------------------------- the work

async def _save_spot(router, g) -> Dict[str, Any]:
    M = _m(router)
    if g["arm"] is None:
        raise ValueError(f"{g['name']} isn't available (is its machine online?)")
    p = await g["arm"].get_end_position()
    router._poses[_key(g)] = {"x": p.x, "y": p.y, "z": p.z, "saved_at": time.time()}
    M._save_poses(router._poses)
    M.LOGGER.info(f"clean-up spot for {g['name']}: ({p.x:.0f}, {p.y:.0f})")
    return {"saved": {"x": round(p.x), "y": round(p.y)}, "arm": g["name"]}


async def _clean_up(router, g, cmd) -> Dict[str, Any]:
    M = _m(router)
    name = M._cap(g["name"])
    spot = _spot(router, g)
    if not spot:
        raise M.NotFound(f"I don't know where the side is yet — move {g['name']} over the spot and say "
                         "“this is the clean up spot”")
    c = router._desk_cam() or router._picker_cam()
    if c is None:
        raise ValueError("there's no camera watching the desk")
    if g["arm"] is None or g["gripper"] is None:
        raise ValueError(f"{name} or its gripper isn't available (is its machine online?)")
    await router._sync_holding(g)
    if g["holding"]:
        raise ValueError(f"{name} is already holding the {g['holding']} — say “let go” first")
    cfg = _cfg(router)
    gap = float(cfg.get("spacing_mm", 80))
    slots = _slots(router, g, spot)
    if not slots:
        raise ValueError(f"the clean-up spot is out of {g['name']}'s reach — teach it again")
    router._action_arms = [g["key"]]
    moved, failed, failed_at, used = [], [], [], []
    misses = 0
    st = router._cleanup_state = {"running": True, "moved": moved, "failed": failed, "started": time.time()}

    def progress(extra=""):
        n = len(moved)
        return f"{name} is cleaning up" + (f" ({n} moved)" if n else "") + (f" — {extra}" if extra else "")

    try:
        async with router._pick_motion(g):
            for _ in range(len(slots) + 6):                     # a hard stop, whatever the camera says
                if len(moved) >= len(slots):
                    break
                router._stage = "look"
                router._set_phase("running", current=progress("looking at the desk"))
                objs = M._number_duplicates(await router._fresh_objects(c))
                seen = [p for p in (_where(router, g, o, c) for o in objs) if p is not None]
                free = [s for s in slots if s not in used and not _near(s, seen, gap * 0.5)]
                here = await g["arm"].get_end_position()
                cands = []
                for o in objs:
                    if not _verified(o):
                        continue
                    p, start, why = _pick_plan(router, g, o, c)
                    if p is None or start is None:
                        if why:
                            M.LOGGER.info(f"clean up: leaving the {_name(o)} ({why})")
                        continue
                    if _near(p, slots, gap * 0.6) or _near(p, failed_at, 50):
                        continue                                  # already at the side, or it didn't work
                    cands.append((math.hypot(p[0] - here.x, p[1] - here.y), o, p, start))
                if not cands:
                    break
                if not free:
                    M.LOGGER.info("clean up: every spot at the side is taken")
                    break
                _, o, p, start = min(cands, key=lambda x: x[0])
                what = _name(o)
                router._set_phase("running", current=progress(f"getting the {what}"))
                try:
                    await start()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    why = M._friendly_error(e)
                    M.LOGGER.info(f"clean up: couldn't pick up the {what}: {why}")
                    failed.append(what)
                    failed_at.append(p)
                    try:
                        if not g["holding"]:
                            await g["gripper"].open()
                    except Exception:
                        pass
                    misses += 1
                    if misses >= 2:
                        break
                    continue
                misses = 0
                slot = free[0]
                held = g["holding"] or what
                place = dict(router._picked_from(g) or {})
                place.update(x=slot[0], y=slot[1])
                if place.get("flange"):
                    place["flange"] = {**place["flange"], "x": slot[0], "y": slot[1]}
                router._stage = "deliver"
                router._set_phase("running", current=progress(f"moving the {held} to the side"))
                if not await router._place_straight(g, place, held):
                    await router._place_planned(g, place, held)
                g["holding"] = None
                router._remember_pick(g, None)
                used.append(slot)
                moved.append(held)
                await asyncio.sleep(0.5)                          # let the camera see the desk without it
    finally:
        st["running"] = False
        st["ended"] = time.time()
    router._stage = "return"
    router._set_phase("running", current=progress("going back to the start"))
    try:
        await router._return_home(g)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        M.LOGGER.warning(f"clean up: {name} couldn't go back to its starting position: {M._friendly_error(e)}")
    if not moved and not failed:
        raise M.NotFound("Nothing to clean up — I don't see anything I know the name of on the desk")
    said = _list(moved)
    router._current = (f"Moved the {said} to the side" if moved else "Didn't move anything") + \
        (f"; couldn't pick up the {_list(failed)}" if failed else "")
    cmd["keep_current"] = True
    return {"moved": moved, "failed": failed}


def _list(xs: List[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1] if xs else ""


async def run(router, cmd: Dict[str, Any], text: str) -> Dict[str, Any]:
    g = router.grabbers.get(cmd["resource_name"]) or _arm(router)
    if g is None:
        raise ValueError("no arm is set up for grabbing")
    if cmd["action"] == "clean_up_spot":
        res = await _save_spot(router, g)
        router._current = "Got it: that's where things go when you say “clean up”"
        cmd["keep_current"] = True
        return res
    return await _clean_up(router, g, cmd)


# ---------------------------------------------------------------- page, DoCommand, state

async def _page_save(router, body):
    if router._action_task is not None and not router._action_task.done():
        raise ValueError("an arm is busy; wait for it to finish")
    g = _arm(router)
    if g is None:
        raise ValueError("no arm is set up for grabbing")
    return await _save_spot(router, g)


async def _page_clear(router, body):
    g = _arm(router)
    existed = g is not None and router._poses.pop(_key(g), None) is not None
    if existed:
        _m(router)._save_poses(router._poses)
    return {"cleared": existed}


async def _page_start(router, body):
    return await router._route("clean up", typed=True)


async def _do(router, command):
    if "cleanup" in command:                 # {"cleanup": true}: same as saying it
        return await router._route("clean up", typed=True)
    v = str(command.get("cleanup_spot") or "show")   # {"cleanup_spot": "save" | "clear" | "show"}
    if v == "save":
        return await _page_save(router, {})
    if v == "clear":
        return await _page_clear(router, {})
    return state(router)


def configure(router, attrs: Dict[str, Any]) -> None:
    router._cleanup_cfg = dict(attrs.get("cleanup") or {})
    router._cleanup_state = getattr(router, "_cleanup_state", None) or {}


def arms_for(router, cmd: Dict[str, Any]) -> List[str]:
    return [cmd["resource_name"]] if cmd["action"] == "clean_up" else []


def label(cmd: Dict[str, Any]) -> str:
    return "Saving the clean-up spot" if cmd["action"] == "clean_up_spot" else "Cleaning up"


def state(router) -> Dict[str, Any]:
    g = _arm(router)
    spot = _spot(router, g) if g else None
    st = getattr(router, "_cleanup_state", None) or {}
    return {"arm": g["name"] if g else None,
            "spot": {"x": round(spot["x"]), "y": round(spot["y"]), "saved_at": spot.get("saved_at")} if spot else None,
            "running": bool(st.get("running")), "moved": list(st.get("moved") or []),
            "failed": list(st.get("failed") or [])}


FEATURE = Feature(
    name="cleanup",
    actions=("clean_up", "clean_up_spot"),
    words=("clean up", "tidy up", "clear the table", "desk everything all mess", "this is the spot here side area",
           "save remember set mark place", "cleanup"),
    configure=configure,
    understand=understand,
    run=run,
    arms_for=arms_for,
    label=label,
    page_actions={"cleanup_save_spot": _page_save, "cleanup_clear_spot": _page_clear, "cleanup_start": _page_start},
    do_commands={"cleanup": _do, "cleanup_spot": _do},
    state=state,
)
