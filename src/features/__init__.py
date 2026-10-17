"""Plug-in features for the voice router.

Each feature lives in its own file in this folder and defines `FEATURE = Feature(...)`. Every file here
is picked up on its own (no list to edit), and main.py only calls the hooks below — so adding or changing
a feature never touches main.py, __init__.py or another feature's file, and several people (or agents)
can work on different features at once without overwriting each other.

What a feature can provide (all optional except `name`):

    name          short id, e.g. "cleanup"; its state shows on /state under features.<name>
    actions       the cmd["action"] values it runs, e.g. ("clean_up",)
    words         extra words the speech recogniser should know ("clean", "tidy", ...), or a
                  function returning them when they change (taught names)
    configure(router, attrs)         called whenever the router is (re)built from its config
    understand(router, words, text)  free-form speech -> a command (router._mk_cmd(action=...)) or None.
                                     Called for everything except "stop", before the built-in intents.
    run(router, cmd, text)           async; does the work inside the router's busy lock. Raise
                                     main.NotFound for "couldn't find it", any other error for a failure.
                                     Set cmd["keep_current"] = True to keep router._current on screen after.
    arms_for(router, cmd)            the arm keys that will move (for recordings and the arm cards)
    label(cmd)                       the line shown while it runs (when the command has no label of its own)
    page_actions  {"name": async fn(router, body) -> dict}   POST /action {"action": "name", ...}
    do_commands   {"key": async fn(router, command) -> dict} DoCommand {"key": ...}
    state(router)                    a small dict for the status page
    priority      int; higher is asked first in understand (default 0, then by file name)
    catalogue(router)                lines describing its commands, for smart mode's list (src/features/llm.py)

A feature that fails to import or raises in a hook is logged and skipped; it can't take the router down.
"""
import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from viam.logging import getLogger

LOGGER = getLogger(__name__)


@dataclass
class Feature:
    name: str
    actions: Tuple[str, ...] = ()
    words: Tuple[str, ...] = ()
    configure: Optional[Callable] = None
    understand: Optional[Callable] = None
    run: Optional[Callable] = None
    arms_for: Optional[Callable] = None
    label: Optional[Callable] = None
    page_actions: Dict[str, Callable] = field(default_factory=dict)
    do_commands: Dict[str, Callable] = field(default_factory=dict)
    state: Optional[Callable] = None
    priority: int = 0
    catalogue: Optional[Callable] = None


def _discover() -> List[Feature]:
    found = []
    for m in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{m.name}")
        except Exception as e:
            LOGGER.error(f"feature '{m.name}' didn't load: {e}")
            continue
        f = getattr(mod, "FEATURE", None)
        if isinstance(f, Feature):
            found.append(f)
    return found


FEATURES: List[Feature] = _discover()
_BY_ACTION: Dict[str, Feature] = {a: f for f in FEATURES for a in f.actions}


def owns(action: Optional[str]) -> bool:
    return bool(action) and action in _BY_ACTION


def words() -> Set[str]:
    out: Set[str] = set()
    for f in FEATURES:
        try:
            ws = f.words() if callable(f.words) else f.words   # a function: words that change (taught names)
        except Exception:
            ws = ()
        out |= {w.lower() for p in ws for w in p.split()}
    return out


def configure(router, attrs: Dict[str, Any]) -> None:
    for f in FEATURES:
        if f.configure:
            try:
                f.configure(router, attrs)
            except Exception as e:
                LOGGER.error(f"feature '{f.name}': config problem: {e}")


def understand(router, words_: List[str], text: str) -> Optional[Dict[str, Any]]:
    for f in sorted(FEATURES, key=lambda f: -f.priority):   # stable: equal priorities stay in name order
        if f.understand:
            try:
                cmd = f.understand(router, words_, text)
            except Exception as e:
                LOGGER.error(f"feature '{f.name}' couldn't read '{text}': {e}")
                continue
            if cmd is not None:
                return cmd
    return None


async def run(router, cmd: Dict[str, Any], text: str) -> Any:
    return await _BY_ACTION[cmd["action"]].run(router, cmd, text)


def arms_for(router, cmd: Dict[str, Any]) -> List[str]:
    f = _BY_ACTION[cmd["action"]]
    return list(f.arms_for(router, cmd)) if f.arms_for else []


def label(cmd: Dict[str, Any]) -> str:
    f = _BY_ACTION[cmd["action"]]
    return f.label(cmd) if f.label else f.name.replace("_", " ").capitalize()


def page_action(router, action: str, body: Dict[str, Any]):
    """The coroutine for a status-page action a feature handles, or None."""
    for f in FEATURES:
        if action in f.page_actions:
            return f.page_actions[action](router, body)
    return None


async def do_command(router, command: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for f in FEATURES:
        for key, fn in f.do_commands.items():
            if key in command:
                return await fn(router, command)
    return None


def catalogue(router) -> List[str]:
    """Every feature's description of its commands, for the LLM (features without one are skipped)."""
    out: List[str] = []
    for f in FEATURES:
        if f.catalogue:
            try:
                out.extend(f.catalogue(router))
            except Exception as e:
                LOGGER.error(f"feature '{f.name}': catalogue problem: {e}")
    return out


def state(router) -> Dict[str, Any]:
    out = {}
    for f in FEATURES:
        if f.state:
            try:
                out[f.name] = f.state(router)
            except Exception as e:
                out[f.name] = {"error": str(e)}
    return out
