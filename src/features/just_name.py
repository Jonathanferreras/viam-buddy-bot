""""Hey buddy … the white cup": just naming a thing means "grab it".

The speech recogniser sometimes loses the first word after "hey buddy" ("grab the white cup" arrives as
"the white cup"), and people also just say the name. When what was heard has no action word at all
("grab", "let go", "reset", "wave", "stop", "play", "clean", …), is short, and names something a camera
can see right now (by the kind the router knows — "cup", "ball", "can" — or by a name taught on the page),
it's treated as "grab the <what was heard>" and goes through the router's normal grab parsing, so colors
and numbers still work ("the white cup 2").
"""
import re
import sys
from typing import Any, Dict, List, Optional

from . import Feature
from .. import status_page, taught

_FILLER = {"the", "a", "an", "that", "this", "one", "please", "buddy", "hey", "um", "uh", "and", "number", "my"}
_MAX_WORDS = 6


def _m(router):
    return sys.modules[type(router).__module__]


def _action_words(M) -> set:
    out = {w for _, ws in getattr(M, "_INTENTS", []) for p in ws for w in p.split()}
    out |= {"play", "game", "clean", "tidy", "clear", "is", "where", "show", "guess", "spot", "jenga", "set"}
    return out - _FILLER - set(getattr(M, "_NOUNS", {})) - set(getattr(M, "_COLORS", []))   # "put the" mustn't make "the" an action


def _visible() -> List[Dict[str, Any]]:
    objs = []
    for cam in status_page.live_cams():
        objs += status_page.live_status(cam).get("objects") or []
    return objs


def understand(router, words: List[str], text: str) -> Optional[Dict[str, Any]]:
    if not getattr(router, "picker", None) or not words or len(words) > _MAX_WORDS:
        return None
    M = _m(router)
    if set(words) & _action_words(M):
        return None                                   # it says what to do: not ours
    if any(c.get("phrases") and any(p in text.lower() for p in c["phrases"]) for c in getattr(router, "commands", [])):
        return None                                   # one of the configured phrases
    rest = [w for w in words if w not in _FILLER and not w.isdigit()]
    if not rest:
        return None
    nouns = getattr(M, "_NOUNS", {})
    colors = set(getattr(M, "_COLORS", []))
    if not all(w in nouns or w in colors or any(w in n.lower().split() for n in taught.names()) for w in rest):
        return None                                   # some word isn't a color or a thing: probably not a name
    said = " ".join(rest)
    things = [w for w in rest if w not in colors]
    if not things:
        return None                                   # just a color ("red"): too thin to move an arm on
    # only if something matching is in view right now
    for o in _visible():
        label = (o.get("tag") or o.get("label") or "").lower()
        kind = (o.get("kind") or "").lower()
        lw = label.split()
        if all(t in lw or t == kind or kind in nouns.get(t, {t}) for t in things):
            cmd = router._understand("grab " + " ".join(words))
            if cmd is not None and cmd.get("action") in ("find_grab", "grab"):
                M.LOGGER.info(f"heard just '{text}': taking it as “grab {said}”")
                return cmd
            return None
    return None


FEATURE = Feature(name="just_name", understand=understand)
