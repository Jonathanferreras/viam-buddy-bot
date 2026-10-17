"""Shell game: guess by tapping the cup with your hand.

While the game waits for a guess (stage "guess", arm 18 holding still over the cups), arm 18's wrist
camera watches the three cups: a hand on one of them, clearly more than on the others, is the guess — the
same as tapping that cup on the status page or saying "left cup". See _tapdetect.py for how a tap is seen.

Optional config, in the router's attributes:
    "cup_tap": {"enabled": true,
                "camera": "cam"}        # arm 18's wrist camera (default: the game's camera)

DoCommand {"cup_taps": "status"} -> what it sees now (how covered each cup is); {"cup_taps": "on" | "off"}.
"""
import asyncio
import contextlib
import sys
import time
from typing import Any, Dict, Optional

from . import Feature

_TASK: Optional[asyncio.Task] = None
_ROUTER = None
_S: Dict[str, Any] = {"watching": False, "covered": None, "cups": None, "last_tap": None, "error": None,
                      "enabled": True, "fps": None}


def _m(router):
    return sys.modules[type(router).__module__]


def _cfg(router) -> Dict[str, Any]:
    return getattr(router, "_cup_tap_cfg", None) or {}


def _cam(router):
    want = _cfg(router).get("camera") or "cam"
    return next((c for c in getattr(router, "live_cams", []) or [] if c.get("camera") == want and c.get("cam") is not None),
                None)


async def _depth(cam):
    """One depth image (mm) from the camera; only the depth picture is asked for when the camera allows it."""
    from .. import vision
    res = None
    if _S.get("depth_only", True):
        try:
            res = await asyncio.wait_for(cam.get_images(filter_source_names=["depth"]), timeout=5)
        except asyncio.CancelledError:
            raise
        except Exception:
            _S["depth_only"] = False                     # this camera / SDK can't: always ask for both
    if res is None:
        res = await asyncio.wait_for(cam.get_images(), timeout=5)
    named = res[0] if isinstance(res, tuple) else res
    for im in named:
        name = (getattr(im, "name", "") or "").lower()
        mime = (getattr(im, "mime_type", "") or "").lower()
        if getattr(im, "data", None) is not None and ("dep" in mime or "depth" in name):
            return await asyncio.to_thread(vision.decode_depth, bytes(im.data))
    if not named:
        raise ValueError("the camera sent no pictures")
    # asked for depth only but got something else: ask for everything
    res = await asyncio.wait_for(cam.get_images(), timeout=5)
    named = res[0] if isinstance(res, tuple) else res
    _, depth = await asyncio.to_thread(vision.split_images, named)
    return depth


def _guessing(router) -> Optional[Dict[str, Any]]:
    g = getattr(type(router), "_GAME", None) or {}
    if g.get("stage") != "guess" or not g.get("answer") or not g.get("cups_view"):
        return None
    t = getattr(router, "_action_task", None)
    if t is not None and not t.done():
        return None                      # something is running (the guess itself, a stop, ...)
    return g


async def _watch_forever():
    from ._tapdetect import CupTaps
    taps, run, fired, peak = None, None, None, None
    frames = []
    while True:
        router = _ROUTER
        try:
            g = _guessing(router) if router is not None and _S["enabled"] else None
            if g is None:
                if _S["watching"]:
                    _S.update(watching=False, covered=None)
                taps, frames = None, []
                await asyncio.sleep(0.4)
                continue
            if g.get("run") == fired:                     # already guessed this round
                await asyncio.sleep(0.4)
                continue
            if g.get("run") != run:
                run, taps, frames = g.get("run"), None, []
            c = _cam(router)
            if c is None:
                _S["error"] = "arm 18's camera isn't set up in the router"
                await asyncio.sleep(2)
                continue
            t0 = time.time()
            depth = await _depth(c["cam"])
            if taps is None:                              # learn the cups with nothing touching them
                frames.append(depth)
                if len(frames) >= 3:
                    taps = await asyncio.to_thread(CupTaps, g["cups_view"], frames)
                    _S.update(watching=True, error=None,
                              cups={k: [round(v * 2) for v in c_] for k, c_ in taps.cups.items()})
                    _m(router).LOGGER.info(f"cup taps: watching the cups at {_S['cups']}")
                await asyncio.sleep(0.15)
                continue
            cup, cov = await asyncio.to_thread(taps.update, depth)
            dt = time.time() - t0
            seen = {k: round(v, 2) for k, v in cov.items()}
            _S.update(covered=seen, fps=round(1.0 / dt, 1) if dt > 0 else None)
            if cup is None:
                peak = seen if max(seen.values() or [0]) >= 0.1 else peak
                continue
            seen = peak if max(seen.values() or [0]) < 0.1 and peak else seen
            if _guessing(router) is None:                 # the round moved on while we looked
                continue
            fired = run
            _S["last_tap"] = {"cup": cup, "at": time.time(), "covered": seen}
            _m(router).LOGGER.info(f"cup taps: a hand tapped the {cup} cup (covered: {seen})")
            try:
                await router._game_from_page("shell_guess", cup)
            except Exception as e:
                fired = None
                _m(router).LOGGER.info(f"cup taps: couldn't take the guess: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _S["error"] = str(e)
            with contextlib.suppress(Exception):
                _m(router).LOGGER.debug(f"cup taps: {e}")
            taps, frames = None, []
            await asyncio.sleep(1.0)


def configure(router, attrs: Dict[str, Any]) -> None:
    global _TASK, _ROUTER
    router._cup_tap_cfg = dict(attrs.get("cup_tap") or {})
    _S["enabled"] = bool(router._cup_tap_cfg.get("enabled", True))
    _ROUTER = router
    if _TASK is None or _TASK.done():
        _TASK = asyncio.get_event_loop().create_task(_watch_forever())


async def _do(router, command):
    v = str(command.get("cup_taps") or "status").lower()
    if v in ("on", "off"):
        _S["enabled"] = v == "on"
    return state(router)


def state(router) -> Dict[str, Any]:
    return {k: _S[k] for k in ("enabled", "watching", "covered", "cups", "last_tap", "error", "fps")}


FEATURE = Feature(
    name="cup_tap",
    configure=configure,
    do_commands={"cup_taps": _do},
    state=state,
)
