"""회전 표현 변환 (numpy, float64). 쿼터니언은 [x, y, z, w] 순서다(MHR·pymomentum·glTF 와 같음).

축각(rotation vector)은 계약 `axis_angle` 과 같은 뜻이다: 방향 = 회전축, 길이 = 회전각(0 ≤ 각 ≤ π, 라디안).
"""

from __future__ import annotations

import numpy as np


def quat_xyzw_to_matrix(q: np.ndarray) -> np.ndarray:
    """단위 쿼터니언 (..., 4) [x, y, z, w] → 회전 행렬 (..., 3, 3). 입력은 정규화한 뒤 쓴다."""
    q = np.asarray(q, dtype=np.float64)
    q = q / np.linalg.norm(q, axis=-1, keepdims=True)
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    out = np.empty(q.shape[:-1] + (3, 3))
    out[..., 0, 0] = 1 - 2 * (y * y + z * z)
    out[..., 0, 1] = 2 * (x * y - w * z)
    out[..., 0, 2] = 2 * (x * z + w * y)
    out[..., 1, 0] = 2 * (x * y + w * z)
    out[..., 1, 1] = 1 - 2 * (x * x + z * z)
    out[..., 1, 2] = 2 * (y * z - w * x)
    out[..., 2, 0] = 2 * (x * z - w * y)
    out[..., 2, 1] = 2 * (y * z + w * x)
    out[..., 2, 2] = 1 - 2 * (x * x + y * y)
    return out


def matrix_to_quat_xyzw(R: np.ndarray) -> np.ndarray:
    """회전 행렬 (..., 3, 3) → 단위 쿼터니언 (..., 4) [x, y, z, w], w ≥ 0 (Shepperd 방식)."""
    R = np.asarray(R, dtype=np.float64)
    flat = R.reshape(-1, 3, 3)
    q = np.empty((flat.shape[0], 4))
    for i, m in enumerate(flat):
        tr = m[0, 0] + m[1, 1] + m[2, 2]
        if tr > 0:
            s = np.sqrt(tr + 1.0) * 2
            q[i] = [(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s]
        elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
            s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
            q[i] = [0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s]
        elif m[1, 1] > m[2, 2]:
            s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
            q[i] = [(m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s]
        else:
            s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
            q[i] = [(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s]
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    q[q[:, 3] < 0] *= -1
    return q.reshape(R.shape[:-2] + (4,))


def rotvec_to_matrix(v: np.ndarray) -> np.ndarray:
    """축각 (..., 3) → 회전 행렬 (..., 3, 3) (Rodrigues)."""
    v = np.asarray(v, dtype=np.float64)
    theta = np.linalg.norm(v, axis=-1)
    small = theta < 1e-12
    axis = np.where(small[..., None], [1.0, 0.0, 0.0], v / np.where(small, 1.0, theta)[..., None])
    x, y, z = axis[..., 0], axis[..., 1], axis[..., 2]
    c, s = np.cos(theta), np.sin(theta)
    C = 1 - c
    out = np.empty(v.shape[:-1] + (3, 3))
    out[..., 0, 0] = c + x * x * C
    out[..., 0, 1] = x * y * C - z * s
    out[..., 0, 2] = x * z * C + y * s
    out[..., 1, 0] = y * x * C + z * s
    out[..., 1, 1] = c + y * y * C
    out[..., 1, 2] = y * z * C - x * s
    out[..., 2, 0] = z * x * C - y * s
    out[..., 2, 1] = z * y * C + x * s
    out[..., 2, 2] = c + z * z * C
    return out


def quat_xyzw_to_rotvec(q: np.ndarray) -> np.ndarray:
    """단위 쿼터니언 (..., 4) → 축각 (..., 3), 회전각 [0, π]."""
    q = np.asarray(q, dtype=np.float64)
    q = q / np.linalg.norm(q, axis=-1, keepdims=True)
    q = np.where(q[..., 3:4] < 0, -q, q)  # w ≥ 0 이면 회전각 ≤ π
    xyz, w = q[..., :3], q[..., 3]
    s = np.linalg.norm(xyz, axis=-1)
    theta = 2 * np.arctan2(s, w)
    # 작은 각에서는 sin(θ/2) ≈ θ/2 라 θ/s → 2
    scale = np.where(s > 1e-12, theta / np.where(s > 1e-12, s, 1.0), 2.0)
    return xyz * scale[..., None]


def matrix_to_rotvec(R: np.ndarray) -> np.ndarray:
    """회전 행렬 (..., 3, 3) → 축각 (..., 3), 회전각 [0, π]."""
    return quat_xyzw_to_rotvec(matrix_to_quat_xyzw(R))


def euler_xyz_to_matrix(e: np.ndarray) -> np.ndarray:
    """MHR(pymomentum) 관절 파라미터의 오일러 [rx, ry, rz] (라디안) → 회전 행렬 R = Rz·Ry·Rx.

    x 축을 먼저 돌리는 고정축 순서다. 실자산 시험(`ai/tests/hmr/test_mhr_asset.py`)이 자산의 관절 상태 계산과 맞는지 확인한다.
    """
    e = np.asarray(e, dtype=np.float64)
    cx, cy, cz = np.cos(e[..., 0]), np.cos(e[..., 1]), np.cos(e[..., 2])
    sx, sy, sz = np.sin(e[..., 0]), np.sin(e[..., 1]), np.sin(e[..., 2])
    out = np.empty(e.shape[:-1] + (3, 3))
    out[..., 0, 0] = cy * cz
    out[..., 0, 1] = cz * sy * sx - sz * cx
    out[..., 0, 2] = cz * sy * cx + sz * sx
    out[..., 1, 0] = sz * cy
    out[..., 1, 1] = sz * sy * sx + cz * cx
    out[..., 1, 2] = sz * sy * cx - cz * sx
    out[..., 2, 0] = -sy
    out[..., 2, 1] = cy * sx
    out[..., 2, 2] = cy * cx
    return out
