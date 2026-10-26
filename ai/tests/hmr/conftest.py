"""MHR glTF 내보내기 시험용 합성 리그 (S15P31A101-88).

실자산(SAM License)은 저장소에 없으므로 관절 5개짜리 리그를 MHR 과 같은 구조로 만든다:
관절 0 = 바닥 원점(파라미터 없음), 관절 1 = pelvis(전역 회전 파라미터 3~5), 관절 2~4 = 자세 회전, 체형 파라미터가 뼈 길이(이동)와
관절 크기를, *_flexible 같은 자세 파라미터가 뼈 길이를 바꾼다. 단위 cm, 쿼터니언 xyzw. 템플릿 역바인드는 파라미터 0 의 순기구학 역이다.
"""

from __future__ import annotations

import numpy as np
import pytest

from simai_ai.hmr.rig import MhrRig, reduce_skin_weights
from simai_ai.hmr.rotation import (
    euler_xyz_to_matrix,
    matrix_to_quat_xyzw,
    quat_xyzw_to_matrix,
)

PARENTS = np.array([-1, 0, 1, 2, 1])
OFFSETS_CM = np.array([[0.0, 0.0, 0.0], [0.0, 90.0, 0.0], [10.0, -5.0, 2.0], [0.0, -40.0, 0.0], [-10.0, -5.0, 1.0]])
P_COLS = 249  # 모델 파라미터 204 + 정체 45 자리(MHR 과 같은 폭)


def momentum_global_states(parents, t_cm, q, s):
    """MHR(pymomentum) 식 그대로의 global 관절 상태: t_g = t_p + R_p·(s_p·t), q_g = q_p·q, s_g = s_p·s. 반환 (t, R, s)."""
    J = len(parents)
    tg, Rg, sg = np.zeros((J, 3)), np.zeros((J, 3, 3)), np.zeros(J)
    R = quat_xyzw_to_matrix(q)
    for j in range(J):
        p = parents[j]
        if p < 0:
            tg[j], Rg[j], sg[j] = t_cm[j], R[j], s[j]
        else:
            tg[j] = tg[p] + Rg[p] @ (sg[p] * t_cm[j])
            Rg[j] = Rg[p] @ R[j]
            sg[j] = sg[p] * s[j]
    return tg, Rg, sg


def joint_params(rig: MhrRig, model_params: np.ndarray) -> np.ndarray:
    mp = np.zeros(rig.parameter_transform.shape[1])
    mp[: len(model_params)] = model_params
    return (rig.parameter_transform @ mp).reshape(rig.joint_count, 7)


def momentum_local(rig: MhrRig, jp: np.ndarray):
    """관절 파라미터 → local (t cm, q, s) (자산의 joint_parameters_to_local_skeleton_state 와 같은 식)."""
    t = jp[:, 0:3] + rig.joint_offsets_cm
    R = quat_xyzw_to_matrix(rig.joint_prerotations) @ euler_xyz_to_matrix(jp[:, 3:6])
    return t, matrix_to_quat_xyzw(R), np.exp2(jp[:, 6])


def reference_vertices_cm(rig: MhrRig, shape_params, model_params) -> tuple[np.ndarray, np.ndarray]:
    """참조 MHR 식 LBS(cm, 리그 좌표): 정체 blendshape → 관절 상태 → 역바인드 → 상위 4 가중치 스키닝. 반환 (정점, 관절 위치)."""
    rest = rig.base_shape_cm + np.einsum("svd,s->vd", rig.shape_vectors_cm, shape_params)
    t, q, s = momentum_local(rig, joint_params(rig, model_params))
    tg, Rg, sg = momentum_global_states(rig.parents, t, q, s)
    inv = rig.inverse_bind_pose
    inv_R = quat_xyzw_to_matrix(inv[:, 3:7]) * inv[:, 7:8, None]
    out = np.zeros_like(rest)
    for k in range(rig.skin_joints.shape[1]):
        j = rig.skin_joints[:, k]
        local = np.einsum("vij,vj->vi", inv_R[j], rest) + inv[j, :3]
        world = np.einsum("vij,vj->vi", Rg[j] * sg[j][:, None, None], local) + tg[j]
        out += rig.skin_weights[:, k, None] * world
    return out, tg


def make_rig(seed: int = 0, vertex_count: int = 40) -> MhrRig:
    rng = np.random.default_rng(seed)
    J = len(PARENTS)
    pre = np.tile([0.0, 0.0, 0.0, 1.0], (J, 1))
    for j in (2, 3, 4):
        q = rng.normal(size=4)
        pre[j] = q / np.linalg.norm(q) * np.sign(q[3])
    P = np.zeros((J * 7, P_COLS))
    P[1 * 7 + 3, 3] = P[1 * 7 + 4, 4] = P[1 * 7 + 5, 5] = 1.0  # 전역 회전 → pelvis
    P[2 * 7 + 3, 6] = P[2 * 7 + 5, 7] = 1.0  # 자세 회전
    P[3 * 7 + 4, 8] = P[3 * 7 + 5, 9] = 1.0
    P[4 * 7 + 3, 10] = P[4 * 7 + 4, 11] = 1.0
    P[3 * 7 + 1, 12] = 5.0  # *_flexible: 자세 파라미터가 뼈 길이(이동)를 바꾼다
    P[3 * 7 + 0, 136] = 3.0  # 체형: 뼈 길이
    P[4 * 7 + 6, 137] = 1.0  # 체형: 관절 크기(손 크기처럼)
    P[2 * 7 + 4, 138] = 0.2  # 체형: 회전(무릎 안쪽 휨처럼)
    scaling = np.zeros(P_COLS, dtype=bool)
    scaling[136:204] = True
    # 템플릿 역바인드 = 파라미터 0 순기구학의 역
    tg, Rg, _ = momentum_global_states(PARENTS, OFFSETS_CM, pre, np.ones(J))
    inv_R = np.transpose(Rg, (0, 2, 1))
    inv_t = -np.einsum("jab,jb->ja", inv_R, tg)
    ibp = np.concatenate([inv_t, matrix_to_quat_xyzw(inv_R), np.ones((J, 1))], axis=1)
    # 관절 근처에 정점, 정점마다 1~5개 영향(5개면 상위 4 로 줄어든다)
    anchor = rng.integers(1, J, size=vertex_count)
    base = tg[anchor] + rng.normal(scale=4.0, size=(vertex_count, 3))
    vi, ji, wi = [], [], []
    for v in range(vertex_count):
        n = int(rng.integers(1, 6))
        js = rng.choice(np.arange(1, J), size=min(n, J - 1), replace=False)  # 관절 0(바닥 원점)에는 가중치가 없다(MHR 과 같음)
        n = len(js)
        ws = rng.uniform(0.05, 1.0, size=n)
        vi += [v] * n
        ji += list(js)
        wi += list(ws / ws.sum())
    skin_j, skin_w, dropped = reduce_skin_weights(np.array(vi), np.array(ji), np.array(wi), vertex_count)
    faces = np.array([[i, (i + 1) % vertex_count, (i + 7) % vertex_count] for i in range(vertex_count)])
    # UV 이음매: 정점 0~4 는 짝수 번째 면에서 다른 UV 를 쓴다
    tex_faces = faces.copy()
    seam = (faces < 5) & (np.arange(len(faces)) % 2 == 0)[:, None]
    tex_faces[seam] += vertex_count
    texcoords = rng.uniform(0, 1, size=(vertex_count + 5, 2))
    return MhrRig(
        joint_names=("body_world", "root", "a", "b", "c"),
        parents=PARENTS.copy(),
        joint_offsets_cm=OFFSETS_CM.copy(),
        joint_prerotations=pre,
        parameter_transform=P,
        scaling_parameters=scaling,
        inverse_bind_pose=ibp,
        base_shape_cm=base,
        shape_vectors_cm=rng.normal(scale=0.5, size=(2, vertex_count, 3)),
        faces=faces,
        skin_joints=skin_j,
        skin_weights=skin_w,
        dropped_weight_max=dropped,
        texcoords=texcoords,
        texcoord_faces=tex_faces,
        asset_sha256="sha256:" + "0" * 64,
        rig_source="synthetic test rig",
    )


def random_model_params(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    mp = np.zeros(204)
    mp[3:6] = rng.uniform(-1.2, 1.2, 3)  # 몸 방향
    mp[6:13] = rng.uniform(-0.9, 0.9, 7)  # 자세 회전 + flexible
    mp[136:139] = rng.uniform(-0.6, 0.6, 3)  # 체형
    return mp


@pytest.fixture
def rig() -> MhrRig:
    return make_rig()
