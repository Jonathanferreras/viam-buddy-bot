""""Hey buddy, grab the screwdriver and put it in the bottom right corner": pick something up and set it
down somewhere you name, in one go.

    grab / pick up / get <thing> and (then) put / place / move / set / drop it (in / on / at / to) <where>
    move / put / place / take <thing> to / in / into / on <where>

<where> is a part of the mat as arm 17's own camera sees it from where it starts (the "Helper" view on the
status page): the center / middle, the left / right side, the top / bottom, or a corner ("the bottom
right corner", "top left"). Top is the far side of the picture, bottom the near side. The spot is worked
out on the blue mat in that picture before the arm moves; if something is already there it slides to the
nearest clear spot. Then the arm finds the thing (same matching as "grab the …": color, name, "cup 2"),
picks it up, sets it down there with its wrist turned as it was picked up, and goes back to its start.

Optional config, in the router's attributes:
    "carry": {"margin": 0.14,      # how far in from the mat's edges the sides and corners are (fraction)
              "clear_mm": 70}      # nothing may be closer than this to where it's set down

DoCommand {"carry_spot": "bottom right corner"}: where that spot is right now (nothing moves).
"""
import asyncio
import math
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import Feature

_PLACE = r"(?:put|place|move|set|drop|leave|slide|bring|take)"
_PREP = r"(?:in|into|on|onto|at|to|toward|towards|over)"
# "grab the screwdriver and put it in the bottom right corner"
_CHAIN = re.compile(rf"^(?P<grab>.+?)\s+(?:and\s+then|and|then)\s+{_PLACE}(?:\s+(?:it|that|them|this))?(?:\s+down)?"
                    rf"(?:\s+{_PREP})?\s+(?P<where>.+)$")
# "move the screwdriver to the bottom right corner"
_ONE = re.compile(rf"^(?:please\s+)?{_PLACE}\s+(?P<thing>.+?)\s+{_PREP}\s+(?P<where>.+)$")
_H = {"left": 0.0, "right": 1.0}
_V = {"top": 0.0, "upper": 0.0, "far": 0.0, "back": 0.0, "bottom": 1.0, "lower": 1.0, "near": 1.0, "front": 1.0}
_MID = {"center", "centre", "middle"}


def _m(router):
    return sys.modules[type(router).__module__]


def _cfg(router) -> Dict[str, Any]:
    return getattr(router, "_carry_cfg", None) or {}


def parse_where(text: str) -> Optional[Dict[str, Any]]:
    """'the bottom right corner' -> {'h': 1.0, 'v': 1.0, 'name': 'bottom right corner'}; None if it names no place."""
    words = re.sub(r"[^a-z ]", " ", text.lower()).split()
    h = next((_H[w] for w in words if w in _H), None)
    v = next((_V[w] for w in words if w in _V), None)
    mid = any(w in _MID for w in words)
    if h is None and v is None and not mid:
        return None
    hv = 0.5 if h is None else h
    vv = 0.5 if v is None else v
    vert = "" if v is None else ("top" if v == 0 else "bottom")
    horiz = "" if h is None else ("left" if h == 0 else "right")
    if h is not None and v is not None:
        name = f"{vert} {horiz} corner"
    elif h is not None or v is not None:
        name = f"{vert or horiz} side" if "side" in words or "edge" in words else (vert or horiz)
    else:
        name = "center"
    return {"h": hv, "v": vv, "name": name}


def split(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """'grab the screwdriver and put it in the bottom right corner' -> ('grab the screwdriver', where)."""
    t = " ".join(re.sub(r"[^a-z0-9' ]", " ", text.lower()).split())
    t = re.sub(r"^(?:hey\s+)?buddy\s+", "", t)
    # polite wrappers: "can you move …", "could you please grab … for me"
    for _ in range(3):
        t = re.sub(r"^(?:can|could|would|will)\s+(?:you|u|ya)\s+|^(?:please|pls|go\s+ahead\s+and|"
                   r"i\s+(?:want|need)\s+you\s+to|i'd\s+like\s+you\s+to)\s+", "", t)
    t = re.sub(r"\s+(?:please|for\s+me|thanks|thank\s+you)$", "", t)
    # speech-to-text hears "to the middle" as "two the middle" / "too the middle"
    t = re.sub(r"\b(?:two|too)\b(?=\s+(?:the\s+)?(?:" + "|".join(sorted({*_H, *_V, *_MID, "corner", "side", "edge"}))
               + r")\b)", "to", t)
    m = _CHAIN.match(t)
    if m:
        where = parse_where(m.group("where"))
        if where:
            return m.group("grab"), where
    m = _ONE.match(t)
    if m:
        where = parse_where(m.group("where"))
        if where:
            return "grab " + m.group("thing"), where
    return None


def understand(router, words: List[str], text: str) -> Optional[Dict[str, Any]]:
    got = split(text)
    if got is None:
        return None
    grab_text, where = got
    inner = router._understand(grab_text)        # the router's own "grab the …" reading (color, name, number)
    if inner is None or inner.get("action") != "find_grab" or not (inner.get("color") or inner.get("noun")):
        return None                                # not a thing it can look for ("move arm 17 to the left")
    what = inner.get("what") or "thing"
    return router._mk_cmd(action="carry", resource_name=inner["resource_name"], name=inner.get("name", ""),
                          color=inner.get("color"), noun=inner.get("noun"), which=inner.get("which"), what=what,
                          where=where, label=f"Moving the {what} to the {where['name']}")


# ---------------------------------------------------------------- where it goes

def mat_box(rgb, step=4):
    """The blue mat in a picture: (x0, y0, x1, y1) in pixels, from where most of its pixels are."""
    import numpy as np
    from PIL import Image
    img = Image.fromarray(rgb[::step, ::step])
    hsv = np.asarray(img.convert("HSV")).astype(float)
    hue, sat, val = hsv[..., 0] * 360 / 255, hsv[..., 1] / 255, hsv[..., 2] / 255
    mat = (hue >= 180) & (hue <= 245) & (sat > 0.28) & (val > 0.10)
    if mat.sum() < 0.05 * mat.size:
        return None, None
    rows, cols = np.nonzero(mat)
    box = (float(np.percentile(cols, 2)) * step, float(np.percentile(rows, 2)) * step,
           float(np.percentile(cols, 98)) * step, float(np.percentile(rows, 98)) * step)
    return box, np.kron(mat, np.ones((step, step), bool))[:rgb.shape[0], :rgb.shape[1]]


def spot_candidates(box, where, margin, n_ring=16):
    """Pixels to try for `where` inside the mat box: the spot itself first, then rings around it."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    u = x0 + w * (margin + (1 - 2 * margin) * where["h"])
    v = y0 + h * (margin + (1 - 2 * margin) * where["v"])
    out = [(u, v)]
    for r in (0.06, 0.12, 0.18, 0.24):
        for k in range(n_ring):
            a = 2 * math.pi * k / n_ring
            out.append((u + r * w * math.cos(a), v + r * h * math.sin(a)))
    return [(a, b) for a, b in out if x0 <= a <= x1 and y0 <= b <= y1]


async def _at_home(router, g) -> Optional[bool]:
    """Is arm `g` at its saved starting position (within 2 degrees per joint)? None if it has none saved."""
    home = (getattr(router, "_poses", {}) or {}).get("home:" + g["key"])
    if not home or g["arm"] is None:
        return None
    q = [float(v) for v in (await g["arm"].get_joint_positions()).values]
    return max(abs(a - float(b)) for a, b in zip(q, home["joints"])) < 2.0


async def _target(router, g, c, where) -> Tuple[List[float], List[List[float]]]:
    """Where to set it down (the arm's base frame), from a fresh picture; and where things already are."""
    import numpy as np
    M = _m(router)
    from .. import vision
    res = await asyncio.wait_for(c["cam"].get_images(), timeout=10)
    named = res[0] if isinstance(res, tuple) else res
    img, depth = await asyncio.to_thread(vision.split_images, named)
    p = (await c["cam"].get_properties()).intrinsic_parameters
    pose = await c["arm_res"].get_end_position()
    rgb = np.asarray(img.convert("RGB"))
    box, mat = mat_box(rgb)
    if box is None:
        raise M.NotFound(f"{M._cap(g['name'])} can't see the mat from here")
    objs = await router._fresh_objects(c)
    taken = [q for q in (router._wrist_point(o, c)[0] for o in objs) if q is not None]
    clear = float(_cfg(router).get("clear_mm", 70))
    for u, v in spot_candidates(box, where, float(_cfg(router).get("margin", 0.14))):
        iu, iv = int(u), int(v)
        win = depth[max(0, iv - 12):iv + 12, max(0, iu - 12):iu + 12]
        onmat = mat[max(0, iv - 12):iv + 12, max(0, iu - 12):iu + 12]
        z = win[(win > 150) & onmat] if onmat.shape == win.shape else win[win > 150]
        if z.size < 20 or onmat.mean() < 0.6:
            continue                                           # not on the mat here (the edge, a thing)
        zc = float(np.median(z))
        cam = [(u - p.center_x_px) * zc / p.focal_x_px, (v - p.center_y_px) * zc / p.focal_y_px, zc]
        q = router._wrist_to_base(c, cam, pose)
        table = router.vision["table_z_mm"] if getattr(router, "vision", None) else None
        if table is not None and not (table - 15 <= q[2] <= table + 40):
            continue                                           # not at table height: a bad depth reading
        if any(math.hypot(q[0] - t[0], q[1] - t[1]) < clear for t in taken):
            continue
        if router._wrist_target_problem({**g, "holding": None}, q, "the mat") is not None:
            continue
        return q, taken
    raise M.NotFound(f"There's no clear spot at the {where['name']} of the mat")


# ---------------------------------------------------------------- the work

def _pick_plan(router, g, o, c):
    """(where it is, a function that starts the pick): the same ways a clicked grab goes."""
    info = router._object_grab_info(o, c)
    if not info:
        return None, None, "grabbing isn't set up for this camera"
    if "why" in info:
        return None, None, info["why"]
    tag = o.get("tag") or o.get("label") or "thing"
    what = tag[:1].lower() + tag[1:]
    if info.get("via") == "wrist":
        p, why = router._wrist_point(o, c)
        h = float((o.get("size_mm") or [0, 0, 0])[2] or 0)
        return p, (lambda: router._pick_at_steps(g, p, what, h, router._wrist_extras(o, c))), why
    if info.get("via") == "link":
        p, extras, why = router._link_target(g, o, c)
        return p, (lambda: router._pick_at_steps(g, p, what, extras["height"], extras)), why
    cam = router._object_cam_mm(o, c)
    return list(g["calibration"].to_arm(cam)), (lambda: router._pick_steps(g, cam, what)), None


async def run(router, cmd: Dict[str, Any], text: str) -> Dict[str, Any]:
    M = _m(router)
    c = router._picker_cam()
    if c is None:
        raise ValueError("no arm with its own camera is set to pick things up (vision.picker)")
    g = router.grabbers[c["arm"]]
    name = M._cap(g["name"])
    where, what = cmd["where"], cmd.get("what") or "thing"
    await router._sync_holding(g)
    if g["holding"]:
        raise ValueError(f"{name} is already holding the {g['holding']} — say “let go” first")
    router._action_arms = [g["key"]]
    if await _at_home(router, g) is False:
        # the corners and sides are those of the view from its starting position (the one on the page)
        router._set_phase("running", current=f"{name} is going to its starting position to look")
        await router._return_home(g)
        await asyncio.sleep(0.6)                               # a fresh picture from there
    router._stage = "look"
    router._set_phase("running", current=f"{name} is finding the {where['name']} and the {what}")
    target, _ = await _target(router, g, c, where)
    objs = M._number_duplicates(await router._fresh_objects(c))
    best, why = router._pick_object(objs, c, cmd.get("color"), cmd.get("noun"), cmd.get("which"))
    if best is None:
        raise M.NotFound(M._cap(why or f"{name} can't see a {what} from where it is"))
    p, start, why = _pick_plan(router, g, best, c)
    if p is None or start is None:
        raise ValueError(why or f"{name} can't reach the {what}")
    if math.hypot(p[0] - target[0], p[1] - target[1]) < float(_cfg(router).get("clear_mm", 70)):
        router._current = f"The {what} is already at the {where['name']}"
        cmd["keep_current"] = True
        return {"moved": False}
    tag = best.get("tag") or best["label"]
    what = tag[:1].lower() + tag[1:]
    M.LOGGER.info(f"carry: the {what} at ({p[0]:.0f}, {p[1]:.0f}) -> the {where['name']} at ({target[0]:.0f}, {target[1]:.0f})")
    async with router._pick_motion(g):
        router._set_phase("running", current=f"{name} is picking up the {what}")
        await start()
        held = g["holding"] or what
        place = dict(router._picked_from(g) or {})
        place.update(x=target[0], y=target[1])
        if place.get("flange"):
            place["flange"] = {**place["flange"], "x": target[0], "y": target[1]}
        router._stage = "deliver"
        router._set_phase("running", current=f"{name} is putting the {held} at the {where['name']}")
        if not await router._place_straight(g, place, held):
            await router._place_planned(g, place, held)
        g["holding"] = None
        router._remember_pick(g, None)
    router._stage = "return"
    router._set_phase("running", current=f"{name} is going back to the start")
    try:
        await router._return_home(g)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        M.LOGGER.warning(f"carry: {name} couldn't go back to its starting position: {M._friendly_error(e)}")
    router._stage = "done"
    router._current = f"Put the {held} at the {where['name']}"
    cmd["keep_current"] = True
    return {"moved": True, "what": held, "where": where["name"], "at": [round(target[0]), round(target[1])]}


async def _do_spot(router, command) -> Dict[str, Any]:
    """DoCommand {"carry_spot": "bottom right corner"}: where that is on the mat right now. Nothing moves."""
    where = parse_where(str(command.get("carry_spot") or ""))
    if where is None:
        return {"error": "say a place: center, left/right side, top/bottom, or a corner like 'bottom right corner'"}
    c = router._picker_cam()
    if c is None:
        return {"error": "no arm with its own camera is set to pick things up"}
    g = router.grabbers[c["arm"]]
    if await _at_home(router, g) is False:
        return {"error": f"{g['name']} isn't at its starting position; the corners and sides are measured from there "
                         "(a carry goes there first). Reset it, then ask again."}
    q, taken = await _target(router, g, c, where)
    return {"where": where["name"], "at_mm": [round(v) for v in q], "arm": g["name"], "things_on_the_mat": len(taken)}


def configure(router, attrs: Dict[str, Any]) -> None:
    router._carry_cfg = dict(attrs.get("carry") or {})


def arms_for(router, cmd: Dict[str, Any]) -> List[str]:
    return [router.picker] if getattr(router, "picker", None) else []


def label(cmd: Dict[str, Any]) -> str:
    return f"Moving the {cmd.get('what') or 'thing'}"


FEATURE = Feature(
    name="carry",
    actions=("carry",),
    words=("put place move set drop slide it in into on onto at to then and",
           "the bottom top left right upper lower center centre middle corner side edge far near front back"),
    configure=configure,
    understand=understand,
    run=run,
    arms_for=arms_for,
    label=label,
    do_commands={"carry_spot": _do_spot},
)
