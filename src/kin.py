"""
Kinematics for the UFactory xArm 850 (the same URDF the Viam driver uses), so short, simple moves
-- straight down onto something, straight back up, turning the wrist -- can be sent to the arm as
joint positions directly, without asking the motion planner for each one.

All lengths in mm, angles in degrees, poses as 4x4 matrices in the arm's base frame (flange pose,
as the arm reports it with get_end_position).
"""
import math
from typing import List, Optional, Sequence

import numpy as np

# joint origin (xyz in m, rpy) for joints 1..6 of uf850.urdf; every joint turns about its own z axis
_JOINTS = [
    ((0, 0, 0.364), (0, 0, 0)),
    ((0, 0, 0), (1.5708, -1.5708, 0)),
    ((0.39, 0, 0), (-3.1416, 0, -1.5708)),
    ((0.15, 0.426, 0), (-1.5708, 0, 0)),
    ((0, 0, 0), (-1.5708, 0, 0)),
    ((0, -0.09, 0), (1.5708, 0, 0)),
]
# joint limits (deg), from the URDF, pulled in 2 degrees so we never ask for the very edge
LIMITS = [(-358, 358), (-130, 130), (-240, 1.5), (-358, 358), (-122, 122), (-358, 358)]


def _rpy(r: float, p: float, y: float) -> np.ndarray:
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return (np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]]) @ np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
            @ np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]]))


_FIXED = []
for _xyz, _r in _JOINTS:
    _A = np.eye(4)
    _A[:3, :3] = _rpy(*_r)
    _A[:3, 3] = np.array(_xyz) * 1000.0
    _FIXED.append(_A)


def fk(q_deg: Sequence[float]) -> np.ndarray:
    """Flange pose (4x4, mm) for joint angles in degrees."""
    T = np.eye(4)
    for A, q in zip(_FIXED, q_deg):
        c, s = math.cos(math.radians(q)), math.sin(math.radians(q))
        Rz = np.array([[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        T = T @ A @ Rz
    return T


def _rotvec(R: np.ndarray) -> np.ndarray:
    """Rotation matrix -> rotation vector (axis * angle, rad)."""
    c = max(-1.0, min(1.0, (np.trace(R) - 1) / 2))
    a = math.acos(c)
    if a < 1e-9:
        return np.zeros(3)
    if a > math.pi - 1e-6:   # 180 degrees: take the axis from the diagonal
        ax = np.sqrt(np.maximum((np.diag(R) + 1) / 2, 0))
        i = int(np.argmax(ax))
        ax[(i + 1) % 3] = math.copysign(ax[(i + 1) % 3], R[i, (i + 1) % 3])
        ax[(i + 2) % 3] = math.copysign(ax[(i + 2) % 3], R[i, (i + 2) % 3])
        return ax / np.linalg.norm(ax) * a
    v = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / (2 * math.sin(a))
    return v * a


def _rot(v: np.ndarray) -> np.ndarray:
    a = float(np.linalg.norm(v))
    if a < 1e-12:
        return np.eye(3)
    k = v / a
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(a) * K + (1 - math.cos(a)) * K @ K


def _err(T: np.ndarray, target: np.ndarray, w_rot: float) -> np.ndarray:
    dp = target[:3, 3] - T[:3, 3]
    dr = _rotvec(target[:3, :3] @ T[:3, :3].T)
    return np.concatenate([dp, dr * w_rot])


def ik(target: np.ndarray, seed: Sequence[float], tol_mm: float = 0.2, tol_deg: float = 0.1,
       iters: int = 200) -> Optional[List[float]]:
    """Joint angles (deg) reaching `target` (flange pose), starting from `seed` (stays on the same
    'branch' as the seed). None if it can't get there within the joint limits."""
    w_rot = 200.0          # 1 rad of orientation error counts like 200 mm
    q = np.array(seed, float)
    lo, hi = np.array([l for l, _ in LIMITS]), np.array([h for _, h in LIMITS])
    lam = 1.0
    for _ in range(iters):
        T = fk(q)
        e = _err(T, target, w_rot)
        if np.linalg.norm(e[:3]) < tol_mm and np.linalg.norm(e[3:]) / w_rot < math.radians(tol_deg):
            return [float(x) for x in q] if np.all(q >= lo) and np.all(q <= hi) else None
        J = np.zeros((6, 6))
        h = 1e-3
        for i in range(6):
            dq = q.copy()
            dq[i] += h
            J[:, i] = (_err(T, target, w_rot) - _err(fk(dq), target, w_rot)) / math.radians(h)   # d(pose)/dq
        # damped least squares step (radians -> degrees)
        step = J.T @ np.linalg.solve(J @ J.T + (lam ** 2) * np.eye(6), e)
        q = np.clip(q + np.degrees(step), lo - 5, hi + 5)
    return None


def pose_down(x: float, y: float, z: float, x_axis: Sequence[float]) -> np.ndarray:
    """Flange pose at (x, y, z) pointing straight down, with its x axis along x_axis (horizontal)."""
    xa = np.array([x_axis[0], x_axis[1], 0.0])
    xa /= np.linalg.norm(xa)
    zd = np.array([0.0, 0.0, -1.0])
    ya = np.cross(zd, xa)
    T = np.eye(4)
    T[:3, 0], T[:3, 1], T[:3, 2], T[:3, 3] = xa, ya, zd, (x, y, z)
    return T


def line(seed: Sequence[float], target: np.ndarray, step_mm: float = 15.0, step_deg: float = 6.0,
         max_jump_deg: float = 12.0) -> Optional[List[List[float]]]:
    """Joint waypoints taking the flange from where `seed` puts it to `target` in a straight line
    (orientation turning evenly on the way). None if any point is out of reach or the arm would
    have to swing a joint suddenly (a different 'branch')."""
    start = fk(seed)
    dist = float(np.linalg.norm(target[:3, 3] - start[:3, 3]))
    turn = math.degrees(float(np.linalg.norm(_rotvec(target[:3, :3] @ start[:3, :3].T))))
    n = max(1, int(math.ceil(max(dist / step_mm, turn / step_deg))))
    rv = _rotvec(target[:3, :3] @ start[:3, :3].T)
    q = list(seed)
    out = []
    for i in range(1, n + 1):
        s = i / n
        T = np.eye(4)
        T[:3, :3] = _rot(rv * s) @ start[:3, :3]
        T[:3, 3] = start[:3, 3] + (target[:3, 3] - start[:3, 3]) * s
        nq = ik(T, q)
        if nq is None or max(abs(a - b) for a, b in zip(nq, q)) > max_jump_deg:
            return None
        out.append(nq)
        q = nq
    return out
