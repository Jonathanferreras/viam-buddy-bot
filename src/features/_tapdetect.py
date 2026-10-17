"""Seeing a hand tap a cup, from arm 18's wrist camera (colour + depth) while it holds still over the cups.

At the start of the guess the camera's depth is learned with nothing touching (a few frames), and each
cup is found in it near where the game says it is: the tops stand well above the mat. After that, every
frame is compared to that: inside each cup's circle (a little wider than the cup), how much of it is now
closer to the camera than it was (a hand or fingers on or just over the cup). A tap is one cup clearly
covered more than the others, for a moment.
"""
import math
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

STEP = 2              # work on every other pixel (720p -> 360x640)
CLOSER_MM = 15.0      # this much nearer than with nothing there counts as "a hand"
ON_FRAC = 0.20        # this much of a cup's circle covered counts as touching it
LEAD = 1.8            # ... and at least this many times more than any other cup
HOLD_S = 0.3          # ... for this long (and at least two frames in a row)
RING = 1.25           # the circle watched, relative to the cup's own size
RELEASE = 0.06        # a cup touched in one frame and all cups this clear in the next is a quick tap


def _nan(depth: np.ndarray) -> np.ndarray:
    d = depth[::STEP, ::STEP].astype(np.float32)
    d[(d <= 0) | (d > 4000)] = np.nan
    return d


class CupTaps:
    def __init__(self, view: Dict[str, Dict[str, float]], frames: List[np.ndarray]):
        """view: {"left": {"u", "v", "r"}, ...} as fractions of the picture (guess_view.json);
        frames: a few depth images (mm) with nothing touching the cups."""
        stack = np.stack([_nan(f) for f in frames])
        with np.errstate(all="ignore"):
            self.base = np.nanmedian(stack, axis=0)
        H, W = self.base.shape
        self.H, self.W = H, W
        vv, uu = np.mgrid[0:H, 0:W]
        self.cups: Dict[str, Tuple[float, float, float]] = {}
        self.masks: Dict[str, np.ndarray] = {}
        self.level: Dict[str, float] = {}
        for name, c in view.items():
            u, v, r = c["u"] * W, c["v"] * H, max(8.0, c["r"] * W)
            u, v, r = self._refine(uu, vv, u, v, r)
            self.cups[name] = (u, v, r)
            m = (uu - u) ** 2 + (vv - v) ** 2 < (RING * r) ** 2
            self.masks[name] = m
            with np.errstate(all="ignore"):
                self.level[name] = float(np.nanpercentile(self.base[m], 20))   # about the cup's top
        # where nothing was known before, compare with the cup's top
        self.ref = {n: np.where(np.isnan(self.base[m]), self.level[n], self.base[m]) for n, m in self.masks.items()}
        self._cand: Optional[str] = None
        self._since = 0.0
        self._count = 0

    def _refine(self, uu, vv, u, v, r):
        """The cup's centre and size in the learned depth: the part near (u, v) standing well above the mat."""
        win = (uu - u) ** 2 + (vv - v) ** 2 < (1.7 * r) ** 2
        ring = ((uu - u) ** 2 + (vv - v) ** 2 > (2.0 * r) ** 2) & ((uu - u) ** 2 + (vv - v) ** 2 < (2.6 * r) ** 2)
        with np.errstate(all="ignore"):
            mat = float(np.nanmedian(self.base[ring]))
        if math.isnan(mat):
            return u, v, r
        cup = win & (self.base < mat - 25)
        n = int(cup.sum())
        if n < 0.25 * math.pi * r * r:
            return u, v, r                                   # not found: keep the game's own estimate
        return float(uu[cup].mean()), float(vv[cup].mean()), float(math.sqrt(n / math.pi))

    def covered(self, depth: np.ndarray) -> Dict[str, float]:
        """How much of each cup's circle is covered now (0..1)."""
        d = _nan(depth)
        out = {}
        for n, m in self.masks.items():
            now = d[m]
            hit = ~np.isnan(now) & (now < self.ref[n] - CLOSER_MM)
            out[n] = float(hit.mean())
        return out

    def update(self, depth: np.ndarray, t: Optional[float] = None) -> Tuple[Optional[str], Dict[str, float]]:
        """Feed one frame: (the cup tapped, if this frame completes a tap; how covered each cup is)."""
        t = time.time() if t is None else t
        f = self.covered(depth)
        order = sorted(f, key=f.get, reverse=True)
        best = order[0]
        second = f[order[1]] if len(order) > 1 else 0.0
        if f[best] >= ON_FRAC and f[best] >= LEAD * max(second, 0.02):
            if self._cand == best:
                self._count += 1
                if self._count >= 2 and t - self._since >= HOLD_S:
                    self._cand = None
                    return best, f                    # a hand resting on it
            else:
                self._cand, self._since, self._count = best, t, 1
        elif self._cand is not None and f[best] < RELEASE:
            cand, self._cand = self._cand, None
            return cand, f                            # a quick tap: on it in one frame, gone the next
        else:
            self._cand = None
        return None, f
