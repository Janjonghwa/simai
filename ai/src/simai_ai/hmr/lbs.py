"""glTF 노드 계층 순기구학과 선형 블렌드 스키닝(LBS) 참조 구현 (numpy, float64).

뷰어(Three.js SkinnedMesh)가 하는 계산과 같은 식이다: 노드 행렬 = T·R·S, global = 부모 global · 노드 행렬,
정점 = Σ w_k · (root · global_k · 역바인드_k) · 휴지 정점. 내보낸 glTF 를 HMR 정점과 맞춰 보는 검증과 시험이 쓴다.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from simai_ai.hmr.rotation import quat_xyzw_to_matrix, rotvec_to_matrix


def topological_order(parents: np.ndarray) -> list[int]:
    """부모가 자식보다 먼저 오는 관절 순서. 순환이 있으면 ValueError."""
    parents = np.asarray(parents)
    children: dict[int, list[int]] = {}
    roots = []
    for j, p in enumerate(parents):
        if p < 0:
            roots.append(j)
        else:
            children.setdefault(int(p), []).append(j)
    order, stack = [], list(reversed(roots))
    while stack:
        j = stack.pop()
        order.append(j)
        stack.extend(reversed(children.get(j, [])))
    if len(order) != len(parents):
        raise ValueError("관절 부모 배열에 순환이 있거나 루트가 없다")
    return order


def node_matrices(translations: np.ndarray, rotations: np.ndarray, scales: np.ndarray) -> np.ndarray:
    """노드 TRS → (J, 4, 4). rotations 는 (J, 3, 3) 행렬 또는 (J, 4) xyzw, scales 는 (J,) 균일 또는 (J, 3)."""
    translations = np.asarray(translations, dtype=np.float64)
    rotations = np.asarray(rotations, dtype=np.float64)
    R = quat_xyzw_to_matrix(rotations) if rotations.shape[-1] == 4 else rotations
    s = np.asarray(scales, dtype=np.float64)
    s = np.repeat(s[:, None], 3, axis=1) if s.ndim == 1 else s
    out = np.zeros((len(translations), 4, 4))
    out[:, :3, :3] = R * s[:, None, :]
    out[:, :3, 3] = translations
    out[:, 3, 3] = 1.0
    return out


def global_matrices(parents: np.ndarray, local: np.ndarray, root: np.ndarray | None = None) -> np.ndarray:
    """노드 행렬 (J, 4, 4) → global (J, 4, 4). root(4x4 또는 3x3 회전)는 루트 노드들의 부모로 곱한다."""
    out = np.empty_like(local)
    top = np.eye(4)
    if root is not None:
        root = np.asarray(root, dtype=np.float64)
        if root.shape == (3, 3):
            top[:3, :3] = root
        else:
            top = root
    for j in topological_order(parents):
        p = parents[j]
        out[j] = (top if p < 0 else out[p]) @ local[j]
    return out


def posed_rotations(
    bind_rotations: np.ndarray, joint_rotations: Sequence[Sequence[float] | None] | np.ndarray
) -> np.ndarray:
    """계약 joint_rotations(관절별 부모 기준 축각, null = 바인드 회전 유지) → 노드 회전 행렬 (J, 3, 3)."""
    bind_R = quat_xyzw_to_matrix(np.asarray(bind_rotations, dtype=np.float64))
    if len(joint_rotations) != len(bind_R):
        raise ValueError(f"joint_rotations 길이 {len(joint_rotations)} != 관절 수 {len(bind_R)}")
    out = bind_R.copy()
    for j, r in enumerate(joint_rotations):
        if r is not None:
            out[j] = rotvec_to_matrix(np.asarray(r, dtype=np.float64))
    return out


def skin_vertices(
    rest_vertices: np.ndarray,
    skin_joints: np.ndarray,
    skin_weights: np.ndarray,
    globals_: np.ndarray,
    inverse_bind: np.ndarray,
) -> np.ndarray:
    """LBS: (V, 3). skin_joints·skin_weights 는 (V, K)."""
    skin_mats = globals_ @ inverse_bind  # (J, 4, 4)
    rest_h = np.concatenate([np.asarray(rest_vertices, dtype=np.float64), np.ones((len(rest_vertices), 1))], axis=1)
    out = np.zeros((len(rest_vertices), 3))
    for k in range(skin_joints.shape[1]):
        w = np.asarray(skin_weights[:, k], dtype=np.float64)
        M = skin_mats[skin_joints[:, k]]  # (V, 4, 4)
        out += w[:, None] * np.einsum("vij,vj->vi", M[:, :3, :], rest_h)
    return out


def skin_directions(
    directions: np.ndarray,
    skin_joints: np.ndarray,
    skin_weights: np.ndarray,
    globals_: np.ndarray,
    inverse_bind: np.ndarray,
) -> np.ndarray:
    """휴지 형상에 더한 변위(예: MHR 자세 보정 형상)가 스키닝 뒤 얼마가 되는지 (선형 부분만, 이동 없음)."""
    skin_mats = globals_ @ inverse_bind
    out = np.zeros((len(directions), 3))
    for k in range(skin_joints.shape[1]):
        M = skin_mats[skin_joints[:, k]][:, :3, :3]
        out += skin_weights[:, k, None] * np.einsum("vij,vj->vi", M, directions)
    return out
