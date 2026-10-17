""""Hey buddy, set up Jenga" (or "build the tower"): arm 18 builds a Jenga tower from blocks lying flat
anywhere on its mat, 3 per layer, each layer turned 90 degrees, then it's your move.

The building is done by jenga/build_tower.py (run as a child process, like the shell game); this file
starts it, follows its "STAGE <id> <text>" and "PROGRESS <placed> <total>" lines for the status page's
Player tab, and stops it on "stop". A stopped build resumes on the same tower next time.

Optional config, in the router's attributes:
    "jenga": {"arm": "arm",     # which arm (its resource name in this router; default "arm" = arm 18)
              "layers": 6}      # tower height (1-18 layers of 3 blocks)

Status page: POST /action {"action": "jenga_start"} / {"action": "jenga_clear"} / {"action": "jenga_putdown"}.
"""
import asyncio
import contextlib
import os
import signal
import sys
import time
from collections import deque
from typing import Any, Dict, List, Optional

from . import Feature

_HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(_HERE, "jenga", "build_tower.py")
PUTDOWN = os.path.join(_HERE, "shell_game", "play.py")
STAGES = ("ready", "scan", "aim", "pick", "place", "check", "wait", "done")
SAY = "set up jenga"


def _m(router):
    return sys.modules[type(router).__module__]


def _cfg(router) -> Dict[str, Any]:
    return getattr(router, "_jenga_cfg", None) or {"arm": "arm", "layers": 6}


def _game(router) -> Dict[str, Any]:
    g = getattr(type(router), "_JENGA", None)      # kept on the class: survives a reconfigure
    if g is None:
        g = type(router)._JENGA = {"stage": None, "detail": "", "progress": None, "error": None, "notice": None,
                                   "since": 0.0, "running": False}
    return g


def _reset(router, **kw):
    g = _game(router)
    g.clear()
    g.update({"stage": None, "detail": "", "progress": None, "error": None, "notice": None,
              "since": time.time(), "running": False, **kw})


def configure(router, attrs):
    c = attrs.get("jenga") or {}
    router._jenga_cfg = {"arm": str(c.get("arm", "arm")), "layers": max(1, min(18, int(c.get("layers", 6))))}
    router._jenga_proc = getattr(router, "_jenga_proc", None)
    _game(router)


def understand(router, words: List[str], text: str):
    w = set(words)
    t = " " + " ".join(words) + " "
    if "jenga" in w or (w & {"build", "stack", "set", "setup"} and w & {"tower", "jenga", "blocks"}):
        if " put it down " in t:
            return None
        return router._mk_cmd(action="jenga", resource_name=_cfg(router)["arm"], name="arm 18",
                              label="Setting up Jenga")
    return None


def arms_for(router, cmd):
    return [cmd.get("resource_name") or _cfg(router)["arm"]]


def label(cmd):
    return "Putting it down" if cmd["action"] == "jenga_putdown" else "Setting up Jenga"


async def _proc(router, args: List[str]) -> (int, Optional[str]):
    """Run a game script, following its stages. (exit code, why it stopped)."""
    g = _game(router)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", *args, cwd=os.path.dirname(args[0]), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)   # own process group: "stop" reaches it all
    router._jenga_proc = proc
    g["running"] = True
    out: deque = deque(maxlen=30)
    log = _m(router).LOGGER
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            s = line.decode(errors="replace").rstrip()
            if not s:
                continue
            out.append(s)
            if s.startswith("STAGE "):
                parts = s.split(" ", 2)
                g.update(stage=parts[1], detail=parts[2] if len(parts) > 2 else "", error=None)
                if router._phase == "running":
                    router._current = g["detail"]
                log.info(f"jenga: {g['detail']}")
            elif s.startswith("PROGRESS "):
                with contextlib.suppress(Exception):
                    g["progress"] = [int(v) for v in s.split()[1:3]]
            else:
                log.debug(f"jenga: {s}")
        rc = await proc.wait()
    except asyncio.CancelledError:
        await _kill(router)
        g.update(stage="stopped", detail="Stopped. If arm 18 is holding a block, tap “Put it down”. "
                                         "Building again carries on with the same tower.")
        raise
    finally:
        router._jenga_proc = None
        g["running"] = False
    why = None
    if rc != 0:
        why = next((l[len("STOPPED:"):].strip() for l in reversed(out) if l.startswith("STOPPED:")), None) \
            or next((l[len("Stopped:"):].strip() for l in reversed(out) if l.startswith("Stopped:")), None) \
            or (out[-1] if out else f"exit code {rc}")
        if "Stationary empty arm required" in why or "holding something" in why:
            why = "Arm 18 is holding something. Tap “Put it down” first."
    return rc, why


async def _kill(router):
    proc = getattr(router, "_jenga_proc", None)
    if proc is None or proc.returncode is not None:
        return
    for sig, wait in ((signal.SIGINT, 6.0), (signal.SIGKILL, 2.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), wait)
            break
        except asyncio.TimeoutError:
            continue


async def run(router, cmd, text):
    if not os.path.exists(SCRIPT):
        raise RuntimeError(f"Jenga isn't installed ({SCRIPT} is missing)")
    if cmd["action"] == "jenga_putdown":
        _reset(router, stage="putdown", detail="Putting it down")
        rc, why = await _proc(router, [PUTDOWN, "putdown"])
        if rc != 0:
            _game(router).update(stage="error", error=why, detail=why)
            raise RuntimeError(f"couldn't put it down: {why}")
        _reset(router, notice="Set the block back on the tower, or lay it flat on the mat, before building again.")
        router._current = "It's down"
        cmd["keep_current"] = True
        return
    layers = _cfg(router)["layers"]
    _reset(router, stage="ready", detail="Getting ready", progress=[0, layers * 3])
    rc, why = await _proc(router, [SCRIPT, "--layers", str(layers)])
    g = _game(router)
    if rc != 0:
        why = why or "the build stopped"
        g.update(stage="error", error=why, detail=why)
        raise RuntimeError(f"Jenga stopped: {why}")
    g.update(stage="done")
    router._current = g["detail"] or "Tower built"
    cmd["keep_current"] = True


async def _page_start(router, body, action="jenga"):
    if router._action_task is not None and not router._action_task.done():
        raise ValueError("an arm is busy; wait for it to finish (or say “Hey buddy, stop”)")
    cmd = router._mk_cmd(action=action, resource_name=_cfg(router)["arm"], name="arm 18", label=label({"action": action}))
    text = "Tapped Build: Jenga" if action == "jenga" else "Tapped Put it down"
    _m(router).LOGGER.info(f"status page: {text}")
    return router._launch(cmd, text, cmd["label"], typed=True)


async def _page_putdown(router, body):
    return await _page_start(router, body, action="jenga_putdown")


async def _page_clear(router, body):
    if _game(router)["running"]:
        raise ValueError("the build is still running")
    _reset(router)
    return {"cleared": True}


def state(router):
    g = _game(router)
    c = _cfg(router)
    return {"name": "Jenga", "arm": "arm 18", "say": SAY, "layers": c["layers"],
            "blurb": f"Lay the blocks flat anywhere on the mat. Buddy builds a {c['layers']}-layer tower, then it's your move.",
            "stages": list(STAGES), **{k: g.get(k) for k in ("stage", "detail", "progress", "error", "notice", "since", "running")}}


FEATURE = Feature(
    name="jenga",
    actions=("jenga", "jenga_putdown"),
    words=("jenga", "set up jenga", "build the tower", "build a tower", "stack the blocks", "setup"),
    configure=configure,
    understand=understand,
    run=run,
    arms_for=arms_for,
    label=label,
    page_actions={"jenga_start": _page_start, "jenga_clear": _page_clear, "jenga_putdown": _page_putdown},
    state=state,
)
