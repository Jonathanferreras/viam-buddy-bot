"""Rename things and choose what counts as "verified", from the status page.

Click a box in either camera feed (or a tile in the grid), type a name, flip Verified, Save. The page posts
    {"action": "label_object", "targets": [{"cam": "arm17-cam", "id": 1000004}, ...],
     "label": "Stapler" | null,   # null keeps the current name
     "verified": true | false}
for every view of that thing (the wrist and desk cameras each have their own id for it). The look of the
thing is remembered (src/taught.py), so the name follows it around and survives restarts.
"Verified" is what clean-up moves and what the page lists under Verified; an object you mark not verified
stays listed (and grabbable by clicking) under Unidentified.

Also:  {"action": "forget_label", "label": "Stapler"}   DoCommand {"taught_labels": true} / {"forget_label": "..."}
Voice: "grab the stapler" works for a taught name the router doesn't otherwise know.
"""
import sys
from typing import Any, Dict, List, Optional

from . import Feature
from .. import status_page, taught

_GRAB = {"grab", "get", "pick", "take", "fetch", "bring", "lift", "hand", "grip"}


def _m(router):
    return sys.modules[type(router).__module__]


def _find(cam: str, oid: int) -> Optional[Dict[str, Any]]:
    return next((o for o in status_page.live_status(cam).get("objects") or [] if o.get("id") == oid), None)


async def _label_object(router, body: Dict[str, Any]) -> Dict[str, Any]:
    targets = body.get("targets") or ([{"cam": body.get("cam"), "id": body.get("id")}] if body.get("id") is not None else [])
    if not targets:
        raise ValueError("nothing selected")
    label = body.get("label")
    label = label.strip() if isinstance(label, str) else None    # not text (null, or 0 via DoCommand): keep the name
    if label is not None and not label:
        raise ValueError("type a name for it")
    if label is not None and len(label) > 40:
        raise ValueError("that name is too long (40 letters at most)")
    verified = bool(body.get("verified", True))
    ident = getattr(_m(router), "_IDENTIFIER", None)
    done, learned = [], False
    for t in targets:
        cam, oid = str(t.get("cam") or ""), t.get("id")
        if oid is None:
            continue
        oid = int(oid)
        obj = _find(cam, oid) if cam else None
        if obj is None:
            continue
        emb = ident.embedding(oid) if ident is not None else None
        r = taught.teach(oid, obj, label, verified, emb)
        learned = learned or r["learned_look"]
        done.append(r["label"])
    if not done:
        raise ValueError("that object isn't in view any more")
    name = done[0]
    _m(router).LOGGER.info(f"status page: taught '{name}' ({'verified' if verified else 'not verified'})"
                           + ("" if learned else "; its look will be learned once it's been named once"))
    return {"label": name, "verified": verified, "learned_look": learned}


async def _forget(router, body: Dict[str, Any]) -> Dict[str, Any]:
    label = str(body.get("label") or body.get("forget_label") or "").strip()
    if not label:
        raise ValueError("which name?")
    return {"forgot": label, "examples": taught.forget(label)}


async def _do(router, command: Dict[str, Any]) -> Dict[str, Any]:
    if "forget_label" in command:
        return await _forget(router, command)
    if "label_object" in command:
        return await _label_object(router, dict(command["label_object"]))
    return {"taught": taught.summary()}


def understand(router, words: List[str], text: str) -> Optional[Dict[str, Any]]:
    """"grab the stapler": a taught name the built-in grab parser doesn't know."""
    if not (set(words) & _GRAB) or not getattr(router, "picker", None):
        return None
    M = _m(router)
    t = " " + " ".join(words) + " "
    for name in sorted(taught.names(), key=len, reverse=True):     # longest first: "blue stapler" before "stapler"
        low = name.lower()
        noun = low.split()[-1]
        if noun in getattr(M, "_NOUNS", {}):
            continue                                               # "cup", "ball": the router handles those
        if f" {low} " in t or f" {noun} " in t:
            color = next((w for w in words if w in getattr(M, "_COLORS", [])), None)
            g = router.grabbers[router.picker]
            return router._mk_cmd(action="find_grab", resource_name=router.picker, name=g["name"], color=color,
                                  noun=noun, which=None, what=low, label=f"Grabbing the {low}")
    return None


def _words():
    return tuple(taught.names()) + tuple(_GRAB)


def state(router) -> Dict[str, Any]:
    return {"taught": taught.summary()}


FEATURE = Feature(
    name="labels",
    words=_words,
    understand=understand,
    page_actions={"label_object": _label_object, "forget_label": _forget},
    do_commands={"taught_labels": _do, "forget_label": _do, "label_object": _do},
    state=state,
)
