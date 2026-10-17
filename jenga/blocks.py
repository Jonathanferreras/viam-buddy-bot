"""Find Jenga blocks (and the tower) in a scan from arm 18's wrist camera. Pure numpy + OpenCV, no robot.

A scan is world points (N x 3, mm) with their colors. The table is fitted as a plane; everything
standing up from it is grouped into clusters on a 6 mm grid; each cluster's top outline gives a
rectangle (center, long-axis angle, length x width) and its height says how it's lying.

A loose block lying flat is about 75 x 25 mm and 15 mm tall. One on its edge (75 x 15, 25 tall)
is reported but not picked. The tower is the cluster at the tower spot.
"""
import math
from collections import defaultdict, deque

import numpy as np


def fit_table(world, rng_seed=42, iters=160, tol=3.0):
    """Table plane z = a*x + b*y + c from the lowest near-horizontal surface (RANSAC, then least squares)."""
    cand = world[(world[:, 2] > -60) & (world[:, 2] < 45)]
    if len(cand) < 500:
        raise RuntimeError('I can’t see enough of the table')
    sample = cand[::max(1, len(cand) // 12000)]
    rng = np.random.default_rng(rng_seed)
    best, best_n = None, 0
    for _ in range(iters):
        tri = sample[rng.choice(len(sample), 3, replace=False)]
        n = np.cross(tri[1] - tri[0], tri[2] - tri[0])
        ln = np.linalg.norm(n)
        if ln < 1:
            continue
        n /= ln
        if abs(n[2]) < .95:
            continue
        d = -n @ tri[0]
        k = int((np.abs(sample @ n + d) < tol).sum())
        if k > best_n:
            best, best_n = (n, d), k
    if best is None or best_n < len(sample) * .4:
        raise RuntimeError('I can’t find the table surface')
    n, d = best
    on = cand[np.abs(cand @ n + d) < tol][::5]
    A = np.column_stack([on[:, :2], np.ones(len(on))])
    coeff = np.linalg.lstsq(A, on[:, 2], rcond=None)[0]
    return coeff


def height_above(world, plane):
    return world[:, 2] - (plane[0] * world[:, 0] + plane[1] * world[:, 1] + plane[2])


def clusters(points, cell=6.0, min_points=60):
    """Connected groups of points on a 3D grid (26-neighbourhood). Returns lists of indices."""
    vox = defaultdict(list)
    for i, key in enumerate(map(tuple, np.floor(points / cell).astype(int))):
        vox[key].append(i)
    seen, out = set(), []
    for start in vox:
        if start in seen:
            continue
        seen.add(start)
        q, idx = deque([start]), []
        while q:
            k = q.popleft()
            idx.extend(vox[k])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        nb = (k[0] + dx, k[1] + dy, k[2] + dz)
                        if nb in vox and nb not in seen:
                            seen.add(nb)
                            q.append(nb)
        if len(idx) >= min_points:
            out.append(idx)
    return out


def describe(pts, hts):
    """Rectangle (from the top outline), height and a guess at what it is."""
    import cv2
    top_h = float(np.percentile(hts, 95))
    top = pts[hts > top_h - 5]
    if len(top) < 10:
        top = pts
    (cx, cy), (w, h), ang = cv2.minAreaRect(top[:, :2].astype(np.float32))
    # the whole footprint, for a fuller outline of a short thing
    (fx, fy), (fw, fh), fang = cv2.minAreaRect(pts[:, :2].astype(np.float32))
    if max(fw, fh) > max(w, h) + 4 and top_h < 40:
        (cx, cy), (w, h), ang = (fx, fy), (fw, fh), fang
    if w < h:                       # long axis along "w"
        w, h, ang = h, w, ang + 90.0
    a = math.radians(ang)
    axis = (math.cos(a), math.sin(a))
    return {'center': [float(cx), float(cy)], 'axis': [float(axis[0]), float(axis[1])],
            'length': float(w), 'width': float(h), 'height': float(top_h), 'points': int(len(pts))}


def kind_of(d, block):
    L, W, H = d['length'], d['width'], d['height']
    bl, bw, bh = block['length_mm'], block['width_mm'], block['height_mm']
    if abs(L - bl) <= 14 and abs(W - bw) <= 8 and abs(H - bh) <= 6:
        return 'block'
    if abs(L - bl) <= 14 and abs(W - bh) <= 6 and abs(H - bw) <= 7:
        return 'block_on_edge'
    if H < 8:
        return 'flat'       # something thin lying on the mat (paper, tape): ignore
    return 'other'


def find(world, rgb, block, tower_xy=None, tower_r=70.0, plane=None):
    """Everything on the table. Returns {'plane', 'blocks', 'tower', 'others'}; each item has the
    rectangle from describe() plus 'kind' (and 'pts': its points, for clearance checks)."""
    plane = fit_table(world) if plane is None else plane
    hts = height_above(world, plane)
    up = (hts > 4) & (hts < 250)
    pts, ph = world[up], hts[up]
    out = {'plane': [float(v) for v in plane], 'blocks': [], 'tower': None, 'others': []}
    tower_idx = []
    for idx in clusters(pts):
        p, h = pts[idx], ph[idx]
        d = describe(p, h)
        d['pts'] = np.column_stack([p[:, :2], h])
        if tower_xy is not None and math.dist(d['center'], tower_xy) < tower_r:
            tower_idx.extend(idx)       # the tower can show up as several pieces: put them together
            continue
        d['kind'] = kind_of(d, block)
        if d['kind'] == 'block':
            out['blocks'].append(d)
        elif d['kind'] != 'flat':
            out['others'].append(d)
    if tower_idx:
        # the tower (or what's left of it): its height says how many layers stand
        p, h = pts[tower_idx], ph[tower_idx]
        d = describe(p, h)
        d['pts'] = np.column_stack([p[:, :2], h])
        d['kind'] = 'tower'
        d['layers'] = int(round(d['height'] / block['height_mm']))
        out['tower'] = d
    return out


def _local(pts_xy, center, axis):
    a = np.asarray(axis)
    perp = np.array([-a[1], a[0]])
    rel = pts_xy - np.asarray(center)
    return rel @ a, rel @ perp


def grip_clear(b, scene, block, finger_mm=(22.0, 28.0), margin=6.0):
    """Can the fingers come down just past both ends of block `b`? Nothing else may be within the
    zones the fingers drop into (finger thickness + margin beyond each end, finger width across)."""
    half = block['length_mm'] / 2
    depth, fw = finger_mm
    others = [o for o in scene['blocks'] + scene['others'] + ([scene['tower']] if scene['tower'] else []) if o is not b]
    for o in others:
        u, v = _local(o['pts'][:, :2], b['center'], b['axis'])
        hit = (np.abs(u) > half - 2) & (np.abs(u) < half + depth + margin) & (np.abs(v) < fw / 2 + margin) & (o['pts'][:, 2] > 2)
        body = (np.abs(u) <= half + depth + margin) & (np.abs(v) < block['width_mm'] / 2 + margin)
        if hit.sum() > 5 or body.sum() > 5:
            return False
    return True


def pick_order(scene, block, tower_xy):
    """Loose flat blocks the fingers can reach, closest to the tower first."""
    ok = [b for b in scene['blocks'] if grip_clear(b, scene, block)]
    return sorted(ok, key=lambda b: math.dist(b['center'], tower_xy))


def tower_spot(scene, block, area, prefer, need_r=95.0, step=10.0):
    """A clear spot for the tower: inside `area` (x0, y0, x1, y1), at least need_r mm from anything,
    as close to `prefer` as possible. None if there's no room."""
    items = scene['blocks'] + scene['others']
    allpts = np.concatenate([o['pts'][:, :2] for o in items]) if items else np.zeros((0, 2))
    best = None
    for x in np.arange(area[0], area[2] + 1e-6, step):
        for y in np.arange(area[1], area[3] + 1e-6, step):
            if len(allpts):
                dmin = float(np.min(np.hypot(allpts[:, 0] - x, allpts[:, 1] - y)))
                if dmin < need_r:
                    continue
            score = math.dist((x, y), prefer)
            if best is None or score < best[0]:
                best = (score, (float(x), float(y)))
    return None if best is None else best[1]


def layer_slots(tower_xy, layer, block, pitch, base_axis=(1.0, 0.0)):
    """Where the three blocks of `layer` (0 = bottom) go: centers and the long-axis direction.
    Even layers run along base_axis, odd layers across it."""
    a = np.asarray(base_axis, float)
    a /= np.linalg.norm(a)
    if layer % 2:
        a = np.array([-a[1], a[0]])
    perp = np.array([-a[1], a[0]])
    c = np.asarray(tower_xy, float)
    return [{'center': (c + perp * pitch * k).tolist(), 'axis': a.tolist()} for k in (-1, 0, 1)]
