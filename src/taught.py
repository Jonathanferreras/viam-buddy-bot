"""Names and "verified" settings taught on the status page, remembered by what the thing looks like.

When you rename something (or mark it verified / not verified), two things are kept:
  * an override for that tracked object, so the change shows at once, and
  * an example: the object's CLIP image embedding (the same one identify.py computes to name it), its size
    and color, saved in taught_labels.json. From then on anything that looks like it — the same thing moved
    elsewhere, picked up and put back, seen after a restart — gets the taught name, ahead of CLIP's guess.

A match needs the picture to be very similar (cosine >= SIM) and the size to be about the same, so a
taught "stapler" doesn't turn every black object into a stapler. Each new rename of the same name adds
another example (up to PER_LABEL), so it gets better at recognising it from other angles.
"""
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "taught_labels.json")
SIM = 0.87          # how alike two pictures must be (CLIP cosine similarity) to count as the same thing
PER_LABEL = 12      # examples kept per name (oldest dropped)
MAX_EXAMPLES = 300

_lock = threading.Lock()
_examples: List[Dict[str, Any]] = []      # {"label", "verified", "emb": np[512], "size_mm", "color", "tall", "t"}
_overrides: Dict[int, Dict[str, Any]] = {}  # tracked object id -> {"label", "verified", "t", "pending": bool}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(PATH) as f:
            data = json.load(f)
        for e in data.get("examples", []):
            e["emb"] = np.asarray(e["emb"], np.float32)
            _examples.append(e)
    except FileNotFoundError:
        pass
    except Exception:
        pass   # a broken file just means nothing taught yet; it's rewritten on the next save


def _save() -> None:
    data = {"examples": [{**e, "emb": [round(float(x), 5) for x in e["emb"]]} for e in _examples]}
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, PATH)


def _size_ok(a: Optional[List[float]], b: Optional[List[float]]) -> bool:
    if not a or not b:
        return True
    for x, y in zip(a[:2], b[:2]):          # length and width within ~60 %
        x, y = max(float(x), 5.0), max(float(y), 5.0)
        if max(x, y) / min(x, y) > 1.6:
            return False
    return True


def _norm(e) -> Optional[np.ndarray]:
    if e is None:
        return None
    e = np.asarray(e, np.float32)
    n = float(np.linalg.norm(e))
    return e / n if n > 0 else None


def teach(oid: Optional[int], obj: Dict[str, Any], label: Optional[str], verified: bool, emb=None) -> Dict[str, Any]:
    """Rename / (un)verify one object. label None keeps the name it has."""
    with _lock:
        _load()
        name = (label or "").strip() or obj.get("label") or "Object"
        name = name[:1].upper() + name[1:]
        if oid is not None:
            _overrides[oid] = {"label": name, "verified": bool(verified), "t": time.time(),
                               "obj": {k: obj.get(k) for k in ("size_mm", "color", "tall")}, "pending": True}
        e = _norm(emb)
        if e is not None:
            _add_example(name, verified, e, obj)
            if oid is not None:
                _overrides[oid]["pending"] = False
        return {"label": name, "verified": bool(verified), "learned_look": e is not None}


def _add_example(name: str, verified: bool, e: np.ndarray, obj: Dict[str, Any]) -> None:
    # the same thing taught again under a new name: the old examples that look like it go
    for x in [x for x in _examples if x["label"] != name and float(x["emb"] @ e) >= SIM
              and _size_ok(x.get("size_mm"), obj.get("size_mm"))]:
        _examples.remove(x)
    for x in _examples:                       # one name, one verified setting
        if x["label"] == name:
            x["verified"] = bool(verified)
    _examples.append({"label": name, "verified": bool(verified), "emb": e, "size_mm": obj.get("size_mm"),
                      "color": obj.get("color"), "tall": bool(obj.get("tall")), "t": time.time()})
    same = [x for x in _examples if x["label"] == name]
    for x in same[:-PER_LABEL]:
        _examples.remove(x)
    del _examples[:-MAX_EXAMPLES]
    _save()


def forget(label: str) -> int:
    with _lock:
        _load()
        n = len(_examples)
        _examples[:] = [x for x in _examples if x["label"].lower() != label.lower()]
        for k in [k for k, v in _overrides.items() if v["label"].lower() == label.lower()]:
            del _overrides[k]
        if len(_examples) != n:
            _save()
        return n - len(_examples)


_NEAR_COLORS = [{"yellow", "orange"}, {"white", "gray", "grey", "silver"}, {"red", "pink"}, {"blue", "purple"}]


def _same_color(a: Optional[str], b: Optional[str]) -> bool:
    a, b = (a or "").lower(), (b or "").lower()
    return not a or not b or a == b or any(a in g and b in g for g in _NEAR_COLORS)


def _agrees(x: Dict[str, Any], o: Dict[str, Any]) -> bool:
    """A look-alike match also has to be the same color, and CLIP must not be sure it's something else:
    a taught "Yellow block" shouldn't rename the red block, nor a screwdriver CLIP is 98 % sure about."""
    if not _same_color(x.get("color"), o.get("color")):
        return False
    kind = x["label"].split()[-1].lower()
    top = (o.get("kind_top") or [[None, 0]])[0]
    if top[0] and top[0] not in ("background", kind) and float(top[1] or 0) >= 0.5:
        return False
    return True


def apply(o: Dict[str, Any], emb) -> Dict[str, Any]:
    """The object with a taught name / verified setting applied, if it has one (else unchanged)."""
    oid = o.get("id")
    with _lock:
        _load()
        hit = _overrides.get(oid) if oid is not None else None
        e = _norm(emb)
        if hit is not None:
            hit["t"] = time.time()
            if hit.get("pending") and e is not None:     # taught before it had been looked at: learn it now
                _add_example(hit["label"], hit["verified"], e, o)
                hit["pending"] = False
        elif e is not None and _examples:
            best, best_sim = None, SIM
            for x in _examples:
                s = float(x["emb"] @ e)
                if s >= best_sim and _size_ok(x.get("size_mm"), o.get("size_mm")) \
                        and bool(x.get("tall")) == bool(o.get("tall")) and _agrees(x, o):
                    best, best_sim = x, s
            hit = best
    if hit is None:
        return o
    o = dict(o)
    o["label"] = hit["label"]
    o["kind"] = hit["label"].split()[-1].lower()     # "Blue stapler" -> "stapler", for "grab the stapler"
    o["kind_conf"] = 1.0
    o["verified"] = bool(hit["verified"])
    o["taught"] = True
    return o


def prune(live_ids) -> None:
    """Forget overrides for objects that left the table a while ago (their looks are kept as examples)."""
    now = time.time()
    with _lock:
        for k in [k for k, v in _overrides.items() if k not in live_ids and now - v["t"] > 60]:
            del _overrides[k]


def summary() -> List[Dict[str, Any]]:
    with _lock:
        _load()
        out: Dict[str, Dict[str, Any]] = {}
        for x in _examples:
            s = out.setdefault(x["label"], {"label": x["label"], "verified": x["verified"], "examples": 0})
            s["examples"] += 1
        return sorted(out.values(), key=lambda s: s["label"].lower())


def names() -> List[str]:
    return [s["label"] for s in summary()]
