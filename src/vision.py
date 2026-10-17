"""
Block finding and camera calibration for the voice router.

find_blocks(): looks for green / red / yellow blocks in an RGB-D camera's color image,
               reads their distance from the aligned depth image, and returns each block's
               3D position in the camera's frame (mm), plus an annotated snapshot (JPEG).

Calibration:   while the arm holds a block, we record (block position seen by the camera,
               arm end position). With 4+ such pairs we solve the rigid transform
               camera -> arm base (Kabsch). The arm position recorded is the pose the arm
               needs to *hold a block at that spot*, so grabbing a block seen on the table
               just means moving the arm to transform(camera point) with the same tool
               orientation used during calibration.
"""

import io
import json
import math
import os
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

# Hue ranges in degrees (0-360). Saturation / value thresholds are 0..1.
DEFAULT_COLORS: Dict[str, Dict[str, Any]] = {
    "red":    {"hues": [[0, 14], [340, 360]], "s_min": 0.45, "v_min": 0.25, "draw": (230, 40, 40)},
    "yellow": {"hues": [[40, 68]],             "s_min": 0.40, "v_min": 0.35, "draw": (240, 200, 0)},
    "green":  {"hues": [[80, 165]],            "s_min": 0.35, "v_min": 0.20, "draw": (30, 190, 70)},
}


# ---------------------------------------------------------------- images

def decode_depth(data: bytes) -> np.ndarray:
    """Viam raw depth (image/vnd.viam.dep): 'DEPTHMAP' + width(u64 BE) + height(u64 BE) + u16 BE mm."""
    if data[:8] != b"DEPTHMAP":
        raise ValueError("depth image is not in Viam's raw depth format")
    w = int.from_bytes(data[8:16], "big")
    h = int.from_bytes(data[16:24], "big")
    arr = np.frombuffer(data[24:24 + w * h * 2], dtype=">u2")
    if arr.size != w * h:
        raise ValueError(f"depth image truncated ({arr.size} of {w * h} pixels)")
    return arr.reshape(h, w).astype(np.float32)


def split_images(named_images) -> Tuple[Image.Image, np.ndarray]:
    """Pick the color and depth images out of camera.get_images() results."""
    color, depth = None, None
    for im in named_images:
        name = (getattr(im, "name", "") or "").lower()
        mime = (getattr(im, "mime_type", "") or "").lower()
        data = getattr(im, "data", None)
        if data is None:
            continue
        if "dep" in mime or "depth" in name:
            depth = decode_depth(bytes(data))
        elif color is None:
            color = Image.open(io.BytesIO(bytes(data))).convert("RGB")
    if color is None:
        raise ValueError("camera returned no color image")
    if depth is None:
        raise ValueError("camera returned no depth image (is it an RGB-D camera with depth enabled?)")
    if depth.shape != (color.height, color.width):
        raise ValueError(f"depth {depth.shape[::-1]} and color {color.size} sizes differ; "
                         "enable align_color_depth on the camera")
    return color, depth


# ---------------------------------------------------------------- detection

def _color_mask(hsv: np.ndarray, spec: Dict[str, Any]) -> np.ndarray:
    h = hsv[..., 0].astype(np.float32) * (360.0 / 255.0)
    s = hsv[..., 1].astype(np.float32) / 255.0
    v = hsv[..., 2].astype(np.float32) / 255.0
    hue_ok = np.zeros(h.shape, dtype=bool)
    for lo, hi in spec["hues"]:
        hue_ok |= (h >= lo) & (h <= hi)
    return hue_ok & (s >= spec["s_min"]) & (v >= spec["v_min"])


def _blobs(mask: np.ndarray, min_px: int) -> List[np.ndarray]:
    """Connected components (4-neighbour) of a boolean mask; returns arrays of (row, col)."""
    h, w = mask.shape
    seen = np.zeros_like(mask)
    out = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys, xs):
        if seen[y0, x0]:
            continue
        q = deque([(y0, x0)])
        seen[y0, x0] = True
        pts = []
        while q:
            y, x = q.popleft()
            pts.append((y, x))
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    q.append((ny, nx))
        if len(pts) >= min_px:
            out.append(np.array(pts))
    return out


MAT_SPEC = {"hues": [[185, 240]], "s_min": 0.35, "v_min": 0.15}   # the blue table mat


def _on_mat_fraction(blob: np.ndarray, mat: np.ndarray, pad: int = 5) -> float:
    """Share of the ring of pixels just around a blob's box that is blue mat."""
    h, w = mat.shape
    r0, c0 = max(0, blob[:, 0].min() - pad), max(0, blob[:, 1].min() - pad)
    r1, c1 = min(h - 1, blob[:, 0].max() + pad), min(w - 1, blob[:, 1].max() + pad)
    ring = np.concatenate([mat[r0, c0:c1 + 1], mat[r1, c0:c1 + 1], mat[r0:r1 + 1, c0], mat[r0:r1 + 1, c1]])
    return float(ring.mean()) if ring.size else 0.0


def find_blocks(color_img: Image.Image, depth_mm: np.ndarray, intr: Dict[str, float],
                colors: Dict[str, Dict[str, Any]], want: Optional[str] = None,
                min_px: int = 120, step: int = 2, on_mat_min: float = 0.35) -> List[Dict[str, Any]]:
    """Find blocks of each color (or just `want`). Works on a 1/step downsampled image for speed.
    Only blobs mostly surrounded by the blue mat count (on_mat_min=0 turns that off).
    Returns candidates sorted by size, each with pixel bbox and 3D camera-frame point (mm)."""
    rgb_small = color_img.resize((color_img.width // step, color_img.height // step))
    hsv = np.asarray(rgb_small.convert("HSV"))
    # "table surface" = the blue mat or the white tabletop; blocks must sit on it
    mat = None
    if on_mat_min > 0:
        s = hsv[..., 1].astype(np.float32) / 255.0
        v = hsv[..., 2].astype(np.float32) / 255.0
        mat = _color_mask(hsv, MAT_SPEC) | ((s < 0.18) & (v > 0.55))
    fx, fy, cx, cy = intr["fx"], intr["fy"], intr["cx"], intr["cy"]
    found = []
    for name, spec in colors.items():
        if want and name != want:
            continue
        mask = _color_mask(hsv, spec)
        for blob in _blobs(mask, max(4, min_px // (step * step))):
            if mat is not None and _on_mat_fraction(blob, mat) < on_mat_min:
                continue
            rows, cols = blob[:, 0] * step, blob[:, 1] * step
            d = depth_mm[rows, cols]
            d = d[(d > 150) & (d < 3000)]           # ignore holes and nonsense
            if d.size < 10:
                continue
            z = float(np.median(d))
            u, v = float(cols.mean()), float(rows.mean())
            found.append({
                "color": name,
                "pixels": int(len(blob) * step * step),
                "bbox": [int(cols.min()), int(rows.min()), int(cols.max()) + step, int(rows.max()) + step],
                "uv": [round(u, 1), round(v, 1)],
                "cam_mm": [(u - cx) * z / fx, (v - cy) * z / fy, z],
            })
    found.sort(key=lambda b: -b["pixels"])
    return found


def annotate(color_img: Image.Image, blocks: List[Dict[str, Any]], chosen: Optional[Dict[str, Any]],
             colors: Dict[str, Dict[str, Any]], banner: str) -> bytes:
    img = color_img.copy()
    dr = ImageDraw.Draw(img)
    for b in blocks:
        rgb = tuple(colors.get(b["color"], {}).get("draw", (255, 255, 255)))
        width = 4 if b is chosen else 1
        dr.rectangle(b["bbox"], outline=rgb, width=width)
        if b is chosen:
            x0, y0 = b["bbox"][0], max(0, b["bbox"][1] - 16)
            dr.rectangle([x0, y0, x0 + 8 + 7 * len(b["color"]), y0 + 15], fill=rgb)
            dr.text((x0 + 4, y0 + 2), b["color"], fill=(0, 0, 0))
    dr.rectangle([0, img.height - 22, img.width, img.height], fill=(0, 0, 0))
    dr.text((8, img.height - 17), banner[:100], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


# ---------------------------------------------------------------- calibration

def kabsch(cam_pts: np.ndarray, arm_pts: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Rigid transform (R, t) minimising |R @ cam + t - arm|."""
    pc, qc = cam_pts.mean(axis=0), arm_pts.mean(axis=0)
    H = (cam_pts - pc).T @ (arm_pts - qc)
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T)) or 1.0
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
    return R, qc - R @ pc


class Calibration:
    """Camera -> arm transform learned from points recorded while the arm holds a block."""

    MIN_POINTS = 4

    def __init__(self, path: str):
        self.path = path
        self.points: List[Dict[str, Any]] = []
        self.R: Optional[np.ndarray] = None
        self.t: Optional[np.ndarray] = None
        self.rms_mm: Optional[float] = None
        self.per_point_mm: List[float] = []
        self.warning: Optional[str] = None
        self.load()

    # persistence
    def load(self):
        try:
            with open(self.path) as f:
                self.points = json.load(f).get("points", [])
        except FileNotFoundError:
            self.points = []
        self.solve()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"points": self.points}, f, indent=2)
        os.replace(tmp, self.path)

    # editing
    def add(self, cam_mm: List[float], arm_pose: Dict[str, float], color: str):
        arm = {k: float(arm_pose[k]) for k in ("x", "y", "z", "o_x", "o_y", "o_z", "theta")}
        self.points.append({"cam": [float(v) for v in cam_mm], "arm": arm, "color": str(color), "t": time.time()})
        self.save()
        self.solve()

    def undo(self):
        if self.points:
            self.points.pop()
            self.save()
            self.solve()

    def clear(self):
        self.points = []
        self.save()
        self.solve()

    # solving
    def solve(self):
        self.R = self.t = self.rms_mm = None
        self.per_point_mm, self.warning = [], None
        if len(self.points) < self.MIN_POINTS:
            return
        P = np.array([p["cam"] for p in self.points])
        Q = np.array([[p["arm"]["x"], p["arm"]["y"], p["arm"]["z"]] for p in self.points])
        # need the points spread out in 3D, or the rotation is a guess
        sv = np.linalg.svd(P - P.mean(axis=0), compute_uv=False)
        if sv[1] < 40 or sv[2] < 15:
            self.warning = ("points are too close together or all at one height; "
                            "record some farther apart and at different heights")
            return
        R, t = kabsch(P, Q)
        err = np.linalg.norm((P @ R.T + t) - Q, axis=1)
        self.R, self.t = R, t
        self.per_point_mm = [round(float(e), 1) for e in err]
        self.rms_mm = round(float(math.sqrt((err ** 2).mean())), 1)
        if self.rms_mm > 15:
            self.warning = (f"average error is {self.rms_mm} mm — too high to grab reliably; "
                            "undo the worst points or re-record")

    @property
    def ready(self) -> bool:
        return self.R is not None and (self.rms_mm or 0) <= 15

    def orientation(self) -> Dict[str, float]:
        """Tool orientation to grab with: the one used while recording (first point)."""
        a = self.points[0]["arm"]
        return {k: a[k] for k in ("o_x", "o_y", "o_z", "theta")}

    def to_arm(self, cam_mm: List[float]) -> List[float]:
        v = self.R @ np.array(cam_mm, dtype=float) + self.t
        return [float(x) for x in v]

    def arm_z_range(self) -> Tuple[float, float]:
        zs = [p["arm"]["z"] for p in self.points]
        return min(zs), max(zs)

    def summary(self) -> Dict[str, Any]:
        return {"points": len(self.points), "needed": self.MIN_POINTS, "ready": self.ready,
                "rms_mm": self.rms_mm, "per_point_mm": self.per_point_mm, "warning": self.warning}


# ---------------------------------------------------------------- live feed (status page)

def _turbo_lut() -> np.ndarray:
    """256-entry 'turbo' colormap (Google's polynomial fit): 0 = deep blue ... 1 = dark red."""
    x = np.linspace(0.0, 1.0, 256)
    r = 0.13572138 + x * (4.61539260 + x * (-42.66032258 + x * (132.13108234 + x * (-152.94239396 + x * 59.28637943))))
    g = 0.09140261 + x * (2.19418839 + x * (4.84296658 + x * (-14.18503333 + x * (4.27729857 + x * 2.82956604))))
    b = 0.10667330 + x * (12.64194608 + x * (-60.58204836 + x * (110.36276771 + x * (-89.90310912 + x * 27.34824973))))
    return (np.clip(np.stack([r, g, b], axis=1), 0, 1) * 255).astype(np.uint8)


_TURBO = _turbo_lut()


def render_live(named_images, prev_range: Optional[Tuple[float, float]] = None,
                fixed_range: Optional[Tuple[float, float]] = None, quality: int = 80) -> Dict[str, Any]:
    """Turn one camera.get_images() result into JPEGs for the live view.
    Color JPEGs pass straight through. Depth is colorized: near = warm (red), far = cool (blue),
    no reading = dark grey. The range auto-fits the scene and is smoothed between frames."""
    color_jpeg, color_size, depth = None, None, None
    for im in named_images:
        name = (getattr(im, "name", "") or "").lower()
        mime = (getattr(im, "mime_type", "") or "").lower()
        data = getattr(im, "data", None)
        if data is None:
            continue
        data = bytes(data)
        if "dep" in mime or "depth" in name:
            depth = decode_depth(data)
        elif color_jpeg is None:
            img = Image.open(io.BytesIO(data))
            color_size = img.size
            if "jpeg" in mime or "jpg" in mime or data[:2] == b"\xff\xd8":
                color_jpeg = data
            else:
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "JPEG", quality=quality)
                color_jpeg = buf.getvalue()

    depth_jpeg, rng = None, prev_range
    if depth is not None:
        if fixed_range:
            rng = (float(fixed_range[0]), float(fixed_range[1]))
        else:
            sample = depth[::4, ::4]
            valid = sample[(sample > 100) & (sample < 8000)]
            if valid.size > 200:
                lo, hi = (float(v) for v in np.percentile(valid, [2, 98]))
                hi = max(hi, lo + 150)
                if prev_range:                      # smooth so the colors don't flicker
                    lo = prev_range[0] * 0.75 + lo * 0.25
                    hi = prev_range[1] * 0.75 + hi * 0.25
                rng = (lo, hi)
        if rng:
            lo, hi = rng
            x = 1.0 - np.clip((depth - lo) / max(1.0, hi - lo), 0.0, 1.0)
            rgb = _TURBO[(x * 255).astype(np.uint8)]
            rgb[depth <= 0] = (24, 24, 28)
            buf = io.BytesIO()
            Image.fromarray(rgb, "RGB").save(buf, "JPEG", quality=quality)
            depth_jpeg = buf.getvalue()

    size = [int(depth.shape[1]), int(depth.shape[0])] if depth is not None else (list(color_size) if color_size else None)
    return {"color": color_jpeg, "depth": depth_jpeg,
            "depth_mm": depth.astype(np.uint16) if depth is not None else None,
            "range_mm": rng, "size": size}


# ---------------------------------------------------------------- tabletop objects (live list)

def _fit_plane(pts: np.ndarray, iters: int = 160, thresh: float = 12.0) -> Optional[Tuple[np.ndarray, float]]:
    """Biggest plane in a point cloud (RANSAC, then least squares on its inliers).
    Returns (unit normal n, offset d) with n.p + d = signed height, positive toward the camera."""
    if len(pts) < 500:
        return None
    rng = np.random.default_rng(7)
    sub = pts[rng.choice(len(pts), min(len(pts), 3000), replace=False)]
    idx = rng.integers(0, len(sub), size=(iters, 3))
    a, b, c = sub[idx[:, 0]], sub[idx[:, 1]], sub[idx[:, 2]]
    n = np.cross(b - a, c - a)
    norm = np.linalg.norm(n, axis=1)
    ok = norm > 1e-6
    if not ok.any():
        return None
    n, a = n[ok] / norm[ok, None], a[ok]
    d = -np.einsum("ij,ij->i", n, a)
    best = int(np.argmax((np.abs(sub @ n.T + d) < thresh).sum(axis=0)))
    inl = sub[np.abs(sub @ n[best] + d[best]) < thresh]
    if len(inl) < 50:
        return None
    c0 = inl.mean(axis=0)
    nn = np.linalg.svd(inl - c0, full_matrices=False)[2][2]
    dd = -float(nn @ c0)
    if dd < 0:                     # camera (the origin) on the positive side
        nn, dd = -nn, -dd
    return nn, dd


def _name_color(rgb: np.ndarray) -> str:
    """Plain color name for a handful of RGB pixels (N x 3, uint8)."""
    hsv = np.asarray(Image.fromarray(rgb.reshape(-1, 1, 3).astype(np.uint8), "RGB").convert("HSV")).reshape(-1, 3)
    h = hsv[:, 0].astype(np.float32) * (360.0 / 255.0)
    s = hsv[:, 1] / 255.0
    v = hsv[:, 2] / 255.0
    colored = (s > 0.35) & (v > 0.18)
    if colored.mean() < 0.35:
        mv = float(np.median(v))
        return "white" if mv > 0.5 else "black" if mv < 0.2 else "gray"
    hh = h[colored]
    hh = np.where(hh > 330, hh - 360, hh)          # keep reds together
    m = float(np.median(hh))
    for name, top in (("red", 14), ("orange", 40), ("yellow", 68), ("green", 165),
                      ("blue", 250), ("purple", 290), ("pink", 330)):
        if m < top:
            return name
    return "red"


def _shift_or(m: np.ndarray) -> np.ndarray:
    o = m.copy()
    o[1:] |= m[:-1]; o[:-1] |= m[1:]; o[:, 1:] |= m[:, :-1]; o[:, :-1] |= m[:, 1:]
    return o


def _dilate(m, r=1):
    for _ in range(r):
        m = _shift_or(m)
    return m


def _erode(m, r=1):
    for _ in range(r):
        m = ~_shift_or(~m)
    return m


def _components(mask: np.ndarray, min_px: int = 1) -> List[np.ndarray]:
    """4-connected components as arrays of (row, col)."""
    h, w = mask.shape
    seen = np.zeros_like(mask)
    out = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if seen[y0, x0]:
            continue
        q = deque([(y0, x0)])
        seen[y0, x0] = True
        pts = []
        while q:
            y, x = q.popleft()
            pts.append((y, x))
            if y > 0 and mask[y - 1, x] and not seen[y - 1, x]:
                seen[y - 1, x] = True; q.append((y - 1, x))
            if y < h - 1 and mask[y + 1, x] and not seen[y + 1, x]:
                seen[y + 1, x] = True; q.append((y + 1, x))
            if x > 0 and mask[y, x - 1] and not seen[y, x - 1]:
                seen[y, x - 1] = True; q.append((y, x - 1))
            if x < w - 1 and mask[y, x + 1] and not seen[y, x + 1]:
                seen[y, x + 1] = True; q.append((y, x + 1))
        if len(pts) >= min_px:
            out.append(np.array(pts))
    return out


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """mask plus every region of ~mask that can't reach the image border."""
    outside = np.zeros_like(mask)
    free = ~mask
    h, w = mask.shape
    q = deque()
    for y in range(h):
        for x in (0, w - 1):
            if free[y, x] and not outside[y, x]:
                outside[y, x] = True; q.append((y, x))
    for x in range(w):
        for y in (0, h - 1):
            if free[y, x] and not outside[y, x]:
                outside[y, x] = True; q.append((y, x))
    while q:
        y, x = q.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < h and 0 <= nx < w and free[ny, nx] and not outside[ny, nx]:
                outside[ny, nx] = True; q.append((ny, nx))
    return ~outside


def _ring(blob: np.ndarray, m: np.ndarray, h: int, w: int, pad: int = 3) -> float:
    """Share of the pixels just outside a blob's bounding box where m is true."""
    r0, c0 = max(0, blob[:, 0].min() - pad), max(0, blob[:, 1].min() - pad)
    r1, c1 = min(h - 1, blob[:, 0].max() + pad), min(w - 1, blob[:, 1].max() + pad)
    ring = np.concatenate([m[r0, c0:c1 + 1], m[r1, c0:c1 + 1], m[r0:r1 + 1, c0], m[r0:r1 + 1, c1]])
    return float(ring.mean()) if ring.size else 0.0


def detect_objects(color_img: Optional[Image.Image], depth_mm: np.ndarray, intr: Dict[str, float],
           colors: Dict[str, Dict[str, Any]], step: int = 2, debug: Optional[dict] = None) -> Dict[str, Any]:
    fx, fy, cx, cy = intr["fx"], intr["fy"], intr["cx"], intr["cy"]
    d = depth_mm[::step, ::step].astype(np.float32)
    hh, ww = d.shape
    vs, us = np.mgrid[0:hh, 0:ww]
    u, v = us * step + step / 2, vs * step + step / 2
    valid = (d > 150) & (d < 4000)
    P = np.stack([(u - cx) * d / fx, (v - cy) * d / fy, d], axis=-1)
    plane = _fit_plane(P[valid], thresh=20.0)
    if plane is None:
        return {"table": False, "objects": []}
    n, off = plane
    H = P @ n + off
    inl = valid & (np.abs(H) < 40)
    if inl.sum() < 0.15 * max(1, valid.sum()):
        return {"table": False, "objects": []}

    # the work surface: the table plane plus anything enclosed by it (at 1/2 of this resolution for speed)
    inl2 = inl[::2, ::2]
    inl2 = _erode(_dilate(inl2, 2), 2)
    big = max(_components(inl2, 1), key=len)
    surf2 = np.zeros_like(inl2)
    surf2[big[:, 0], big[:, 1]] = True
    work2 = _dilate(_fill_holes(_dilate(surf2, 1)), 4)   # generous: things at the table's edge count too
    work = np.repeat(np.repeat(work2, 2, 0), 2, 1)[:hh, :ww]

    # rays through each pixel hit the table plane here (for footprint sizes)
    rays = np.stack([(u - cx) / fx, (v - cy) / fy, np.ones_like(d)], axis=-1)
    t = -off / np.clip(rays @ n, -1e9, -1e-6)          # ray . n < 0 for a plane facing the camera
    G = rays * t[..., None]                              # table point under each pixel

    if color_img is None:
        return {"table": True, "objects": []}
    small = color_img.resize((ww, hh), Image.BILINEAR) if color_img.size != (ww, hh) else color_img
    rgb = np.asarray(small.convert("RGB"))
    hsv = np.asarray(small.convert("HSV"))
    hue = hsv[..., 0] * (360.0 / 255.0)
    sat = hsv[..., 1] / 255.0
    val = hsv[..., 2] / 255.0
    mat = (hue >= 180) & (hue <= 245) & (sat > 0.28) & (val > 0.10)
    light = (sat < 0.22) & (val > 0.40)
    # large light regions are the table itself; small ones (enclosed by the mat) are things on it
    # (eroded first, so a cup touching the white table through a thin bridge stays separate)
    light4 = light[::2, ::2]
    core4 = _erode(light4, 2)
    table_white4 = np.zeros_like(light4)
    for comp in _components(core4, 1):
        if len(comp) > 0.02 * light4.size:
            table_white4[comp[:, 0], comp[:, 1]] = True
    table_white4 = _dilate(table_white4, 3) & light4
    table_white = np.repeat(np.repeat(table_white4, 2, 0), 2, 1)[:hh, :ww] & light

    fg_color = work & ~mat & ~table_white
    fg_color = _dilate(_erode(fg_color, 1), 1)                       # drop seams and specks
    tall = work & valid & (H > 50) & (H < 350)
    tall = _dilate(_erode(tall, 2), 2)
    fg = fg_color | tall
    if debug is not None:
        debug.update(work=work, mat=mat, table_white=table_white, fg_color=fg_color, tall=tall, H=H)

    surface = mat | table_white
    objects = []
    min_px = max(8, 160 // (step * step))
    parts = []
    for blob0 in _components(fg, min_px):
        r0, c0 = blob0[:, 0], blob0[:, 1]
        if _ring(blob0, surface, hh, ww) < 0.6:
            continue                                                  # not sitting on the mat or table
        hb0 = H[r0, c0][valid[r0, c0]]
        if hb0.size >= 5 and (float(np.percentile(hb0, 10)) > 60 or float(np.median(hb0)) > 90):
            continue                                                  # floating above the table: part of an arm
        parts.extend(_split_stacked(blob0, H, valid, min_px))
    e1 = np.cross(n, [1.0, 0.0, 0.0]); e1 /= np.linalg.norm(e1); e2 = np.cross(n, e1)   # axes in the table plane
    for blob, rest in parts:                                          # rest: height of what it lies on (0 = table)
        rows, cols = blob[:, 0], blob[:, 1]
        hb = H[rows, cols][valid[rows, cols]]
        # its own height (for a thing lying on another, measured from that thing's top)
        height = max(0.0, float(np.percentile(hb, 90)) - rest) if hb.size >= 5 else 0.0
        zs = d[rows, cols][valid[rows, cols]]
        z = float(np.median(zs)) if zs.size else None
        tall = height > 40
        axis, top_pt = None, None   # its long direction and its top centre (camera frame), for the gripper
        top_abs = float(np.percentile(hb, 90)) if hb.size >= 5 else 0.0
        if tall and z:
            # seen at an angle, a tall thing's outline says little about its footprint: use its width
            length = width = float((cols.max() - cols.min() + 1) * step * z / fx)
        else:
            # flat: where the pixel rays hit the table plane is where it is
            g = G[rows, cols]
            c2 = np.stack([g @ e1, g @ e2], axis=1)
            mid = c2.mean(axis=0)
            c2 = c2 - mid
            V = np.eye(2)
            if len(c2) >= 3:
                V = np.linalg.eigh(np.cov(c2.T))[1]
                c2 = c2 @ V
            ext = (np.percentile(c2, 95, axis=0) - np.percentile(c2, 5, axis=0)) / 0.9
            length, width = float(max(ext)), float(min(ext))
            # centre of its footprint, by the middle of its extent (not the pixel average, which a
            # screwdriver's big handle pulls toward itself), raised to its top
            # (from the depth points of its top face: the table-plane footprint above is stretched away
            # from the camera, since rays over a raised thing land on the table behind it)
            okp = valid[rows, cols] & (H[rows, cols] >= top_abs - max(6.0, 0.35 * height))
            face = P[rows[okp], cols[okp]]
            if len(face) >= 10:
                f2 = np.stack([face @ e1, face @ e2], axis=1)
                fmid = f2.mean(axis=0)
                fr = (f2 - fmid) @ V
                lo5, hi95 = np.percentile(fr, 5, axis=0), np.percentile(fr, 95, axis=0)
                ctr = fmid + V @ ((lo5 + hi95) / 2)
                base_pt = face.mean(axis=0)
                top_pt = base_pt + e1 * (ctr[0] - fmid[0]) + e2 * (ctr[1] - fmid[1])
                top_pt = top_pt + n * (top_abs - float(np.mean(face @ n + off)))   # onto its top
            else:
                lo5, hi95 = np.percentile(c2, 5, axis=0), np.percentile(c2, 95, axis=0)
                mid2 = mid + V @ ((lo5 + hi95) / 2)
                foot = g.mean(axis=0) + e1 * (mid2[0] - (g @ e1).mean()) + e2 * (mid2[1] - (g @ e2).mean())
                top_pt = foot + n * top_abs
            if length > 1.3 * max(width, 1):
                major = V[:, int(np.argmax(ext))]
                axis = major[0] * e1 + major[1] * e2
            if length > 7 * max(width, 1):
                continue                                              # a cable
        if length > 300 or length < 12:
            continue                                                  # laptop-sized, or a speck
        # what the gripper's fingers close on: the top few cm of it (a tall thing is gripped near its top),
        # measured from the depth points themselves. An upside-down cup is grabbed by its narrow base,
        # not its wide rim on the table.
        grip = width
        cut_off = rows.min() == 0 or cols.min() == 0 or rows.max() >= hh - 1 or cols.max() >= ww - 1
        if tall and hb.size >= 20 and not cut_off:   # (half out of the picture: don't trust a narrow reading)
            top_h = float(np.percentile(hb, 95))
            ok = valid[rows, cols]
            band = ok & (H[rows, cols] >= top_h - 35) & (H[rows, cols] <= top_h + 15)
            pts = P[rows[band], cols[band]]
            if len(pts) >= 12:
                c2 = np.stack([pts @ e1, pts @ e2], axis=1)
                c2 = c2 - c2.mean(axis=0)
                V = np.linalg.eigh(np.cov(c2.T))[1]
                c2 = c2 @ V
                ext = (np.percentile(c2, 98, axis=0) - np.percentile(c2, 2, axis=0)) / 0.96
                grip = min(float(min(ext)), width)
                top_pt = pts.mean(axis=0)
                if max(ext) > 1.3 * max(min(ext), 1):
                    major = V[:, int(np.argmax(ext))]
                    axis = major[0] * e1 + major[1] * e2
        px = rgb[rows, cols]
        color_name = _name_color(px)
        block = None
        hv = hsv[rows, cols]
        for cname, spec in colors.items():
            if _color_mask(hv, spec).mean() > 0.35 and length < 90:
                block = cname
                break
        label = f"{block.capitalize()} block" if block else f"{color_name.capitalize()} object"
        objects.append({
            "label": label, "color": block or color_name, "block": block,
            "swatch": {"white": "#f2f2f4", "gray": "#9a9aa0", "black": "#2c2c2e"}.get(color_name) if not block
                      else "#%02x%02x%02x" % tuple(int(x) for x in np.median(px, axis=0)),
            "bbox": [int(cols.min() * step), int(rows.min() * step), int((cols.max() + 1) * step), int((rows.max() + 1) * step)],
            "uv": [float(cols.mean() * step), float(rows.mean() * step)],
            "size_mm": [round(length), round(width), round(height)],
            "grip_mm": round(grip),
            "axis_cam": [round(float(a), 4) for a in axis] if axis is not None else None,
            "top_cam": [round(float(a), 1) for a in top_pt] if top_pt is not None else None,
            "tall": tall,
            "dist_mm": round(z) if z else None,
            "on_top": rest > 0,
        })
    return {"table": True, "objects": objects}


def _split_stacked(blob: np.ndarray, H: np.ndarray, valid: np.ndarray, min_px: int) -> List[Tuple[np.ndarray, float]]:
    """Something lying on something else (a screwdriver across a row of blocks) touches it, so the two come
    out as one blob. If part of a blob stands clearly above a flat lower layer, with a step between the two
    (not a smooth rise, like a can on its side), that part becomes an object of its own.
    Returns [(pixels, height of what they rest on)]; 0 = resting on the table."""
    rows, cols = blob[:, 0], blob[:, 1]
    ok = valid[rows, cols]
    if ok.sum() < 3 * min_px:
        return [(blob, 0.0)]
    h = H[rows, cols]
    hv = h[ok]
    base = float(np.percentile(hv, 35))                     # top of the lower thing
    if base < 8:                                            # the "lower thing" is just the table
        return [(blob, 0.0)]
    low = hv[hv <= base + 6]
    flat = float(np.mean(np.abs(low - base) < 6)) if low.size else 0.0     # lower layer has a flat top
    step = float(np.mean((hv > base + 6) & (hv <= base + 14)))           # few heights in between: a step
    upper = ok & (h > base + 14)
    frac = float(upper.sum()) / float(ok.sum())
    if not (0.05 <= frac <= 0.6) or step > 0.08 or flat < 0.8:
        return [(blob, 0.0)]
    mask = np.zeros(H.shape, bool)
    mask[rows[upper], cols[upper]] = True
    tops = _components(_dilate(_erode(mask, 1), 1), max(4, min_px // 2))
    if not tops:
        return [(blob, 0.0)]
    taken = np.zeros(H.shape, bool)
    for t in tops:
        taken[t[:, 0], t[:, 1]] = True
    lower = blob[~taken[rows, cols]]
    return ([(lower, 0.0)] if len(lower) >= min_px else []) + [(t, base) for t in tops]


class ObjectTracker:
    """Gives detections stable ids between frames, hides one-frame flickers, and keeps an
    object listed for a couple of frames after it's missed."""

    def __init__(self, max_jump_px: float = 45, keep: int = 2, confirm: int = 2):
        self.tracks: Dict[int, Dict[str, Any]] = {}
        self.next_id = 1
        self.max_jump, self.keep, self.confirm = max_jump_px, keep, confirm

    def update(self, objs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        pairs = sorted((math.dist(o["uv"], t["obj"]["uv"]), i, tid)
                       for i, o in enumerate(objs) for tid, t in self.tracks.items())
        used, unmatched = set(), set(self.tracks)
        for dist, i, tid in pairs:
            if dist > self.max_jump:
                break
            if i in used or tid not in unmatched:
                continue
            used.add(i)
            unmatched.discard(tid)
            t = self.tracks[tid]
            t.update(obj=objs[i], miss=0, hits=t["hits"] + 1)
        for i, o in enumerate(objs):
            if i not in used:
                self.tracks[self.next_id] = {"obj": o, "miss": 0, "hits": 1}
                self.next_id += 1
        for tid in unmatched:
            self.tracks[tid]["miss"] += 1
            if self.tracks[tid]["miss"] > self.keep:
                del self.tracks[tid]
        shown = [{**t["obj"], "id": tid} for tid, t in self.tracks.items() if t["hits"] >= self.confirm]
        return sorted(shown, key=lambda o: (o["uv"][0], o["uv"][1]))
