""""Hey buddy, clean up": the picker arm takes every verified thing on the desk (the ones the camera has
put a name to, i.e. the page's "Verified" group) and sets them down at the side, one by one, then goes
back to its starting position.

Where "the side" is gets taught once: move the arm over the spot and say "this is the clean up spot"
(or press the page's button, or DoCommand {"cleanup_spot": "save"}). It's kept in wave_poses.json.

Things are set down starting at that spot and working outward, ring by ring, sweeping from left to down as
the arm's own camera sees the desk from its starting position (the view on the page): the spot itself
first, then the places one step out to its left, down-left and below it, then the next ring, and so on.
Nothing is set down where something already is: before starting, the arm goes to its starting position and
looks with its own camera (the table camera often can't see the side), and a place is used only if nothing
stands in the thing's footprint there, the desk is really there (not off its edge) and the camera can see
it. Things put down during the run are remembered too, and the place must fit the thing's size, so it just
keeps going outward until everything is moved (or it runs out of desk it can reach and see). Things already
right at the spot, or next to what it has put down, count as cleaned up and are left alone.
Check without moving: DoCommand {"cleanup_side": true} (with the arm at its starting position) lists where
the next things would go.

Optional config, in the router's attributes:
    "cleanup": {"arm": "arm17-arm",      # which arm (default: vision.picker)
                "grow": ["left", "down"], # which way to work outward, in the arm camera's picture
                "step_mm": 40,           # how finely places are tried (ring spacing)
                "max_radius_mm": 450,    # farthest from the spot it puts things
                "area_mm": 60,           # things this close to the spot already count as cleaned up
                "max_items": 20}         # most things moved in one go

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
    if o.get("verified") is not None:                    # set on the status page (src/taught.py)
        return bool(o["verified"])
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


_DIRS = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}   # (picture right, picture down)


def _axes(router, side: Optional["SideMap"]) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """The two directions to work outward in, in the arm's base frame (x, y), from the "grow" setting."""
    right, down = (side.right, side.down) if side is not None else ((0.0, -1.0), (-1.0, 0.0))   # arm 17's view
    out = []
    for name in (list(_cfg(router).get("grow") or ["left", "down"]) + ["left", "down"])[:2]:
        a, b = _DIRS.get(str(name).lower(), (0, 0))
        out.append((a * right[0] + b * down[0], a * right[1] + b * down[1]))
    if math.hypot(*out[0]) < 0.5 or math.hypot(*out[1]) < 0.5 or abs(out[0][0] * out[1][0] + out[0][1] * out[1][1]) > 0.5:
        out = [(-right[0], -right[1]), down]                       # a bad setting: left and down
    return out[0], out[1]


def _places(router, spot, side: Optional["SideMap"]) -> List[Tuple[float, float]]:
    """Where things can go, nearest first: the spot, then ring after ring outward, each swept from the first
    "grow" direction to the second (left to down by default)."""
    cfg, v = _cfg(router), router.vision
    step = max(15.0, float(cfg.get("step_mm", 40)))
    far = float(cfg.get("max_radius_mm", 450))
    (ax, ay), (bx, by) = _axes(router, side)
    x0, y0 = spot["x"], spot["y"]
    out = [(x0, y0)]
    k = 1
    while k * step <= far:
        rho = k * step
        n = max(2, int(math.ceil(rho * math.pi / 2 / step)) + 1)
        for i in range(n):
            t = (math.pi / 2) * i / (n - 1)
            dx, dy = math.cos(t) * ax + math.sin(t) * bx, math.cos(t) * ay + math.sin(t) * by
            out.append((x0 + rho * dx, y0 + rho * dy))
        k += 1
    return [p for p in out if v["reach_min_mm"] + 20 <= math.hypot(p[0], p[1]) <= v["reach_max_mm"] - 20]


def _near(p, pts, r) -> bool:
    return any(math.hypot(p[0] - q[0], p[1] - q[1]) < r for q in pts)


# ---------------------------------------------------------------- what's already at the side
# The side is usually at the edge of the table camera's picture (or outside its work area), where it
# can't see what's there. So before cleaning up, the picker arm looks from its starting position with its
# own camera, and every slot is checked against those heights: nothing standing in the thing's footprint,
# the desk actually there (not off its edge), and the camera able to see it at all.

_PAD_MM = 8.0


def _radius(o: Optional[Dict[str, Any]]) -> float:
    """The clearance a thing needs around its centre (mm): half its footprint's longest side."""
    s = (o or {}).get("size_mm") or [0, 0, 0]
    return max(25.0, min(110.0, 0.5 * max(float(s[0] or 0), float(s[1] or 0))))


def _footprint(router, g, o, c) -> Tuple[Optional[Tuple[float, float]], float, float]:
    """(axis, half length, radius) of what a thing covers on the desk. A long thin thing (a fork, a
    screwdriver) is a stadium along its axis, since it's set down turned the way it was picked up; anything
    else (or one whose direction isn't known) is a circle (axis None, half length 0)."""
    s = o.get("size_mm") or [0, 0, 0]
    L, W = max(float(s[0] or 0), float(s[1] or 0)), min(float(s[0] or 0), float(s[1] or 0))
    if L > 1.5 * max(W, 1.0):
        axis = None
        try:
            info = router._object_grab_info(o, c) or {}
            if info.get("via") == "wrist":
                axis = router._wrist_extras(o, c).get("axis")
            elif info.get("via") == "link":
                axis = router._link_target(g, o, c)[1].get("axis")
        except Exception:
            axis = None
        if axis:
            n = math.hypot(axis[0], axis[1]) or 1.0
            rad = max(12.0, W / 2)
            return (axis[0] / n, axis[1] / n), max(0.0, L / 2 - rad), rad
    return None, 0.0, _radius(o)


def _seg(c, axis, half):
    if axis is None or half <= 0:
        return (c[0], c[1]), (c[0], c[1])
    return (c[0] - axis[0] * half, c[1] - axis[1] * half), (c[0] + axis[0] * half, c[1] + axis[1] * half)


def _pt_seg(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _seg_seg(a, b, c, d) -> float:
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2, d3, d4 = cross(c, d, a), cross(c, d, b), cross(a, b, c), cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 * d2 != 0 and d3 * d4 != 0:
        return 0.0                                                     # they cross
    return min(_pt_seg(a, c, d), _pt_seg(b, c, d), _pt_seg(c, a, b), _pt_seg(d, a, b))


def _gap(c1, f1, c2, f2) -> float:
    """Clear distance (mm) between two footprints at centres c1, c2 (negative: they overlap)."""
    a, b = _seg(c1, f1[0], f1[1])
    c, d = _seg(c2, f2[0], f2[1])
    return _seg_seg(a, b, c, d) - f1[2] - f2[2]


class SideMap:
    """Heights over the desk, in the arm's base frame, from one picture of the picker arm's camera."""

    TALL_MM = 4.5        # this much above the mat counts as something there (bare mat reads within ~2 mm;
                         # a plastic fork lying flat stands 6-8 mm)

    def __init__(self, xyz, table_z: float, mm_per_pt: float, right=(0.0, -1.0), down=(-1.0, 0.0)):
        self.xyz, self.table, self.mm_per_pt = xyz, float(table_z), float(mm_per_pt)
        self.right, self.down = right, down       # the picture's right and down, in the base frame (x, y)

    def check(self, x: float, y: float, r: float, axis=None, half: float = 0.0) -> Optional[str]:
        """None if a thing of radius r (a stadium `half` either way along `axis`, if given) can go down at
        (x, y); otherwise why it can't."""
        import numpy as np
        X, Y, Z = self.xyz[:, 0], self.xyz[:, 1], self.xyz[:, 2]
        if axis is not None and half > 0:
            t = np.clip((X - x) * axis[0] + (Y - y) * axis[1], -half, half)
            inside = (X - x - t * axis[0]) ** 2 + (Y - y - t * axis[1]) ** 2 < r * r
        else:
            half = 0.0
            inside = (X - x) ** 2 + (Y - y) ** 2 < r * r
        z = Z[inside]
        expected = (math.pi * r * r + 4 * half * r) / (self.mm_per_pt ** 2)
        if z.size < 0.85 * expected:               # (nearly) all of the footprint must be in view
            return "the camera can't see there"
        if float(np.median(z)) < self.table - 25:
            return "that's off the edge of the desk"
        tall = z[z > self.table + self.TALL_MM]
        if tall.size > 10:                         # bare mat has none; a fork handle edge has dozens
            h = float(np.percentile(tall, 80)) - self.table
            return f"something about {h:.0f} mm tall is already there"
        if abs(float(np.median(z)) - self.table) > 15:
            return "the desk isn't flat there"
        return None


def build_side_map(rgb, depth, intr, mount, pose, ov_matrix, table_default: float, step: int = 3) -> Optional[SideMap]:
    """SideMap from a wrist-camera picture. intr (fx, fy, cx, cy); mount and pose as Viam poses
    (dicts / objects with x y z o_x o_y o_z theta); ov_matrix the router's orientation-vector rotation."""
    import numpy as np
    if depth is None:
        return None
    fx, fy, cx, cy = [float(v) for v in intr]
    g = lambda p, k: float(p[k] if isinstance(p, dict) else getattr(p, k))
    vv, uu = np.mgrid[0:depth.shape[0]:step, 0:depth.shape[1]:step]
    z = depth[::step, ::step].astype(float)
    ok = (z > 150) & (z < 3000)
    if ok.sum() < 500:
        return None
    u, v, z = uu[ok], vv[ok], z[ok]
    cam = np.stack([(u - cx) * z / fx, (v - cy) * z / fy, z], 1)
    Rm = ov_matrix(g(mount, "o_x"), g(mount, "o_y"), g(mount, "o_z"), g(mount, "theta"))
    Rp = ov_matrix(g(pose, "o_x"), g(pose, "o_y"), g(pose, "o_z"), g(pose, "theta"))
    tm = np.array([g(mount, "x"), g(mount, "y"), g(mount, "z")])
    tp = np.array([g(pose, "x"), g(pose, "y"), g(pose, "z")])
    xyz = (cam @ Rm.T + tm) @ Rp.T + tp
    table = table_default
    if rgb is not None and rgb.shape[:2] == depth.shape[:2]:
        c = rgb[::step, ::step][ok].astype(float)
        mat = (c[:, 2] > c[:, 0] + 30) & (c[:, 2] > c[:, 1] + 10)      # the blue mat
        if mat.sum() > 500:
            table = float(np.median(xyz[mat, 2]))
    mm_per_pt = float(np.median(z)) / fx * step
    R = Rp @ Rm

    def unit(v):
        n = math.hypot(float(v[0]), float(v[1])) or 1.0
        return (float(v[0]) / n, float(v[1]) / n)
    return SideMap(xyz, table, mm_per_pt, right=unit(R[:2, 0]), down=unit(R[:2, 1]))


async def _at_home(router, g) -> Optional[bool]:
    """Is arm `g` at its saved starting position (within 2 degrees per joint)? None if it has none saved."""
    home = (getattr(router, "_poses", {}) or {}).get("home:" + g["key"])
    if not home or g["arm"] is None:
        return None
    q = [float(v) for v in (await g["arm"].get_joint_positions()).values]
    return max(abs(a - float(b)) for a, b in zip(q, home["joints"])) < 2.0


async def _side_map(router, g) -> Optional[SideMap]:
    """A SideMap from arm `g`'s own camera as it is now; None if it has no camera with depth."""
    import numpy as np
    from .. import vision
    c = router._picker_cam()
    if c is None or c.get("arm") != g["key"] or not c.get("mount"):
        return None
    res = await asyncio.wait_for(c["cam"].get_images(), timeout=10)
    named = res[0] if isinstance(res, tuple) else res
    img, depth = await asyncio.to_thread(vision.split_images, named)
    p = (await c["cam"].get_properties()).intrinsic_parameters
    pose = await c["arm_res"].get_end_position()
    rgb = np.asarray(img.convert("RGB")) if img is not None else None
    return await asyncio.to_thread(build_side_map, rgb, depth, (p.focal_x_px, p.focal_y_px, p.center_x_px, p.center_y_px),
                                   c["mount"], pose, _m(router)._ov_matrix, router.vision["table_z_mm"])


def _slot_problem(slot, fp, side: Optional[SideMap], placed, seen) -> Optional[str]:
    """Why a thing with footprint fp (axis, half length, radius — see _footprint; a bare number is a
    circle's radius) can't go down at `slot` (None: it can). placed: [(where, footprint)] put down this
    time; seen: [(where, footprint)] of things the table camera sees (not the one being moved)."""
    fp = (None, 0.0, float(fp)) if isinstance(fp, (int, float)) else fp
    pad = _PAD_MM                                   # set-down accuracy, so things don't land touching
    for q, fq in placed:
        if _gap(slot, fp, q, fq) < pad:
            return "something was just put there"
    for q, fq in seen:
        if _gap(slot, fp, q, fq) < pad:
            return "something is already there"
    return side.check(slot[0], slot[1], fp[2] + pad, fp[0], fp[1]) if side is not None else None


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
    if not _places(router, spot, None):
        raise ValueError(f"the clean-up spot is out of {g['name']}'s reach — teach it again")
    router._action_arms = [g["key"]]
    side = None
    if router._picker_cam() is not None and router._picker_cam().get("arm") == g["key"]:
        # look at the side with the arm's own camera first (the table camera often can't see it)
        if await _at_home(router, g) is False:
            router._set_phase("running", current=f"{name} is going to its starting position to look at the side")
            await router._return_home(g)
            await asyncio.sleep(0.6)                              # a fresh picture from there
        router._stage = "look"
        router._set_phase("running", current=f"{name} is checking what's already at the side")
        try:
            side = await _side_map(router, g)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            M.LOGGER.warning(f"clean up: couldn't look at the side with {g['name']}'s camera: {M._friendly_error(e)}")
    if side is None:
        M.LOGGER.warning(f"clean up: {g['name']} couldn't look at the side; only the table camera checks where things go")
    places = _places(router, spot, side)
    area = float(cfg.get("area_mm", 60))
    moved, failed, failed_at, no_room = [], [], [], []
    placed: List[Tuple[Tuple[float, float], float]] = []
    misses = 0
    st = router._cleanup_state = {"running": True, "moved": moved, "failed": failed, "no_room": no_room,
                                  "started": time.time()}

    def progress(extra=""):
        n = len(moved)
        return f"{name} is cleaning up" + (f" ({n} moved)" if n else "") + (f" — {extra}" if extra else "")

    try:
        async with router._pick_motion(g):
            most = int(cfg.get("max_items", 20))
            for _ in range(most + 6):                           # a hard stop, whatever the camera says
                if len(moved) >= most:
                    break
                router._stage = "look"
                router._set_phase("running", current=progress("looking at the desk"))
                objs = M._number_duplicates(await router._fresh_objects(c))
                seen = [(o, w) for o, w in ((o, _where(router, g, o, c)) for o in objs) if w is not None]
                here = await g["arm"].get_end_position()
                cands, waiting = [], []
                for o in objs:
                    if not _verified(o):
                        continue
                    p, start, why = _pick_plan(router, g, o, c)
                    if p is None or start is None:
                        if why:
                            M.LOGGER.info(f"clean up: leaving the {_name(o)} ({why})")
                        continue
                    if math.hypot(p[0] - spot["x"], p[1] - spot["y"]) < area \
                            or any(_gap(p, (None, 0.0, 0.0), q, fq) < 30 for q, fq in placed) \
                            or _near(p, failed_at, 50):
                        continue                                  # already at the side, or it didn't work
                    if _name(o) in no_room:
                        continue
                    r = _footprint(router, g, o, c)
                    others = [(w, (None, 0.0, _radius(x))) for x, w in seen if x is not o]
                    slot = next((s for s in places if _slot_problem(s, r, side, placed, others) is None), None)
                    if slot is None:
                        waiting.append(o)                         # nowhere free at the side that it fits
                        continue
                    cands.append((math.hypot(p[0] - here.x, p[1] - here.y), o, p, start, slot, r))
                if not cands:
                    for o in waiting:
                        M.LOGGER.info(f"clean up: no room left at the side for the {_name(o)}")
                        no_room.append(_name(o))
                    break
                _, o, p, start, slot, r = min(cands, key=lambda x: x[0])
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
                placed.append((slot, r))
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
    if not moved and not failed and not no_room:
        raise M.NotFound("Nothing to clean up — I don't see anything I know the name of on the desk")
    said = _list(moved)
    router._current = (f"Moved the {said} to the side" if moved else "Didn't move anything") + \
        (f"; couldn't pick up the {_list(failed)}" if failed else "") + \
        (f"; no room left at the side for the {_list(no_room)}" if no_room else "")
    cmd["keep_current"] = True
    return {"moved": moved, "failed": failed, "no_room": no_room}


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


async def _do_side(router, command):
    """{"cleanup_side": true}: what's at each slot at the side, seen from the arm's starting position.
    Doesn't move anything; the arm has to be at its start already."""
    g = _arm(router)
    if g is None:
        raise ValueError("no arm is set up for grabbing")
    spot = _spot(router, g)
    if not spot:
        raise ValueError("no clean-up spot saved yet")
    if await _at_home(router, g) is False:
        raise ValueError(f"{g['name']} isn't at its starting position (it looks at the side from there)")
    side = await _side_map(router, g)
    if side is None:
        raise ValueError(f"{g['name']} has no camera with depth to look with")
    r = float(command.get("radius_mm") or 35)
    n = int(command.get("count") or 8)
    placed, nxt = [], []
    for _ in range(n):                         # where n things of that size would go, one after another
        s = next((s for s in _places(router, spot, side) if _slot_problem(s, r, side, placed, []) is None), None)
        if s is None:
            break
        placed.append((s, (None, 0.0, r)))
        nxt.append([round(s[0]), round(s[1])])
    return {"arm": g["name"], "table_z_mm": round(side.table, 1), "spot_mm": [round(spot["x"]), round(spot["y"])],
            "at_the_spot": side.check(spot["x"], spot["y"], r + _PAD_MM) or "clear",
            "next_places_mm": nxt, "radius_mm": r}


async def _do(router, command):
    if "cleanup_side" in command:
        return await _do_side(router, command)
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
    do_commands={"cleanup": _do, "cleanup_spot": _do, "cleanup_side": _do},
    state=state,
)
