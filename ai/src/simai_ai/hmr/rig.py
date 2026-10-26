"""MHR 리그 추출과 사람별 체형 굽기 (S15P31A101-88, docs/design/mhr-skinned-gltf.md).

MHR 자산(`mhr_model.pt`, TorchScript, SAM License: 재배포 금지)에서 스키닝에 필요한 것만 numpy 로 꺼낸다:
관절 127개(이름·부모·이동 오프셋·사전 회전), 파라미터 변환(모델 파라미터 → 관절 파라미터), 템플릿 역바인드,
정체 blendshape(기본 형상 + 45 성분), 면, UV, 스킨 가중치(정점당 상위 4개로 줄여 정규화).

MHR 관절 상태는 관절마다 [이동 t(3), 회전 q(4, xyzw), 균일 크기 s(1)] 이고 단위는 cm 다.
local t = 관절 파라미터 이동 + 오프셋, local q = 사전 회전 · 오일러(관절 파라미터 회전), local s = 2^(관절 파라미터 크기).
global 은 t_g = t_p + R_p·(s_p·t), q_g = q_p·q, s_g = s_p·s 로, glTF 노드 TRS 계층(행렬 T·R·S 곱)과 같은 계산이다.

사람별 자산은 노드의 휴지 TRS 에 그 사람의 체형(뼈 길이 scale, 손 크기, 무릎 안쪽 휨)을 굽고, 역바인드는 템플릿 것을
그대로 쓴다. 정점은 정체 blendshape 를 적용한 템플릿 자세 형상이다. 이렇게 하면 glTF 스키닝(노드 global · 역바인드)이
MHR 의 선형 스키닝과 같은 식이 된다(체형을 정점에 미리 굽는 방식은 관절마다 다른 크기를 섞어 정확하지 않다).

자산을 읽을 때만 torch 를 import 한다. 굽기·내보내기·포즈 변환은 numpy 만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from simai_ai.hmr.rotation import (
    euler_xyz_to_matrix,
    matrix_to_quat_xyzw,
    quat_xyzw_to_matrix,
)

# deploy/weights.json 의 sam-3d-body-dinov3-mhr (facebook/sam-3d-body-dinov3 @11aaa34, assets/mhr_model.pt)
MHR_ASSET_SHA256 = "352e271a6c42729c68554ceaea0c955e866970160c31e35506d782dc0f7377bc"
MHR_RIG_SOURCE = "facebookresearch/MHR mhr_model.pt @ facebook/sam-3d-body-dinov3@11aaa34"
MHR_JOINT_COUNT = 127
MHR_VERTEX_COUNT = 18439
MHR_FACE_COUNT = 36874
PELVIS_JOINT_INDEX = 1  # 자산 관절 이름 "root". 관절 0 "body_world" 는 바닥 원점이고 늘 단위 변환이다
MHR_MODEL_PARAM_COUNT = 204  # 전역 이동 3 + 전역 회전 3 + 몸 자세 130 + 체형 scale 68 (모델 출력 mhr_model_params)
MHR_SCALE_COUNT = 68
MHR_BODY_POSE_COUNT = 130
CM_TO_M = 0.01
SKIN_INFLUENCES = 4  # glTF JOINTS_0/WEIGHTS_0 한 벌


@dataclass(frozen=True)
class MhrRig:
    """MHR 자산에서 꺼낸 리그(사람과 무관한 템플릿). 길이 단위는 자산 그대로 cm."""

    joint_names: tuple[str, ...]
    parents: np.ndarray  # (J,) int, 루트 −1, 부모 인덱스 < 자기 인덱스
    joint_offsets_cm: np.ndarray  # (J, 3)
    joint_prerotations: np.ndarray  # (J, 4) xyzw
    parameter_transform: np.ndarray  # (J*7, P) 모델 파라미터(204 + 정체 45 자리) → 관절 파라미터
    scaling_parameters: np.ndarray  # (P,) bool, 체형(scale) 파라미터
    inverse_bind_pose: np.ndarray  # (J, 8) 템플릿 역바인드 관절 상태 [t(cm), q, s]
    base_shape_cm: np.ndarray  # (V, 3)
    shape_vectors_cm: np.ndarray  # (S, V, 3) 정체 blendshape
    faces: np.ndarray  # (F, 3) int
    skin_joints: np.ndarray  # (V, 4) int
    skin_weights: np.ndarray  # (V, 4) float64, 행 합 1
    dropped_weight_max: float  # 상위 4개로 줄일 때 버린 가중치 합의 최대값(정규화 전)
    texcoords: np.ndarray | None = None  # (T, 2)
    texcoord_faces: np.ndarray | None = None  # (F, 3) int
    asset_sha256: str = ""
    rig_source: str = MHR_RIG_SOURCE

    @property
    def joint_count(self) -> int:
        return len(self.parents)

    @property
    def vertex_count(self) -> int:
        return len(self.base_shape_cm)


@dataclass(frozen=True)
class PersonRig:
    """한 사람의 체형을 구운 리그(glTF 로 쓸 값). 길이 단위 m, 좌표는 MHR 자산 좌표 = glTF Y-up."""

    rig: MhrRig
    rest_vertices_m: np.ndarray  # (V, 3) 정체 blendshape 를 적용한 템플릿 자세 정점
    local_translations_m: np.ndarray  # (J, 3) 노드 휴지 이동
    local_rotations: np.ndarray  # (J, 4) xyzw 노드 휴지(바인드) 회전 = 포즈가 null 인 관절에 둘 회전
    local_scales: np.ndarray  # (J,) 노드 균일 크기
    inverse_bind_matrices: np.ndarray  # (J, 4, 4) 템플릿 역바인드(m)


def joint_parents_sha256(parents: np.ndarray | list[int]) -> str:
    """계약 skeleton.joint_parents_sha256: 부모 배열을 공백 없는 JSON 으로 적은 UTF-8 바이트의 sha256."""
    text = json.dumps([int(p) for p in parents], separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def reduce_skin_weights(
    vert_indices: np.ndarray, joint_indices: np.ndarray, weights: np.ndarray, vertex_count: int, k: int = SKIN_INFLUENCES
) -> tuple[np.ndarray, np.ndarray, float]:
    """희소 스킨 가중치(정점·관절·가중치 3열)를 정점당 상위 k 개로 줄이고 합 1 로 정규화한다.

    반환: (joints (V, k) int, weights (V, k) float64, 버린 가중치 합의 최대값). 빈 칸은 관절 0·가중치 0 이다.
    영향이 하나도 없는 정점이 있으면 ValueError(지어내지 않는다).
    """
    vert_indices = np.asarray(vert_indices, dtype=np.int64)
    joint_indices = np.asarray(joint_indices, dtype=np.int64)
    weights = np.asarray(weights, dtype=np.float64)
    order = np.lexsort((-weights, vert_indices))  # 정점별로 가중치 큰 순
    v_sorted, j_sorted, w_sorted = vert_indices[order], joint_indices[order], weights[order]
    starts = np.searchsorted(v_sorted, np.arange(vertex_count))
    counts = np.bincount(v_sorted, minlength=vertex_count)
    if (counts == 0).any():
        raise ValueError(f"스킨 가중치가 없는 정점 {int((counts == 0).sum())}개")
    rank = np.arange(len(v_sorted)) - starts[v_sorted]
    keep = rank < k
    out_j = np.zeros((vertex_count, k), dtype=np.int64)
    out_w = np.zeros((vertex_count, k), dtype=np.float64)
    out_j[v_sorted[keep], rank[keep]] = j_sorted[keep]
    out_w[v_sorted[keep], rank[keep]] = w_sorted[keep]
    total = np.bincount(v_sorted, weights=w_sorted, minlength=vertex_count)
    kept = out_w.sum(axis=1)
    dropped = float(np.max(total - kept)) if len(total) else 0.0
    out_w /= kept[:, None]
    return out_j, out_w, max(dropped, 0.0)


def skel_state_to_matrix(state: np.ndarray, length_scale: float = 1.0) -> np.ndarray:
    """관절 상태 (..., 8) [t, q(xyzw), s] → 4x4 행렬 T(t·length_scale)·R(q)·S(s)."""
    state = np.asarray(state, dtype=np.float64)
    out = np.zeros(state.shape[:-1] + (4, 4))
    out[..., :3, :3] = quat_xyzw_to_matrix(state[..., 3:7]) * state[..., 7:8, None]
    out[..., :3, 3] = state[..., :3] * length_scale
    out[..., 3, 3] = 1.0
    return out


def load_mhr_rig(asset_path: str | Path, expected_sha256: str | None = MHR_ASSET_SHA256) -> MhrRig:
    """MHR 자산(TorchScript)에서 리그를 꺼낸다. sha256 이 다르면 ValueError(다른 판의 관절 순서를 조용히 쓰지 않음).

    torch 가 필요하다(분석 워커 환경). 자산 자체는 저장소·산출물에 넣지 않는다(SAM License).
    """
    asset_path = Path(asset_path)
    if expected_sha256 is not None:
        got = file_sha256(asset_path)
        if got != expected_sha256.removeprefix("sha256:"):
            raise ValueError(f"MHR 자산 sha256 불일치: {got} (기대 {expected_sha256})")
    import torch

    model = torch.jit.load(str(asset_path), map_location="cpu")
    ch = model.character_torch

    def arr(t) -> np.ndarray:
        return t.detach().cpu().numpy()

    lbs = ch.linear_blend_skinning
    vertex_count = int(arr(ch.mesh.rest_vertices).shape[0])
    skin_j, skin_w, dropped = reduce_skin_weights(
        arr(lbs.vert_indices_flattened), arr(lbs.skin_indices_flattened), arr(lbs.skin_weights_flattened), vertex_count
    )
    texcoords = arr(ch.mesh.texcoords).astype(np.float64)
    texcoord_faces = arr(ch.mesh.texcoord_faces).astype(np.int64)
    return MhrRig(
        joint_names=tuple(model.get_joint_names()),
        parents=arr(ch.skeleton.joint_parents).astype(np.int64),
        joint_offsets_cm=arr(ch.skeleton.joint_translation_offsets).astype(np.float64),
        joint_prerotations=arr(ch.skeleton.joint_prerotations).astype(np.float64),
        parameter_transform=arr(ch.parameter_transform.parameter_transform).astype(np.float64),
        scaling_parameters=arr(ch.parameter_transform.scaling_parameters).astype(bool),
        inverse_bind_pose=arr(lbs.inverse_bind_pose).astype(np.float64),
        base_shape_cm=arr(ch.blend_shape.base_shape).astype(np.float64),
        shape_vectors_cm=arr(ch.blend_shape.shape_vectors).astype(np.float64),
        faces=arr(ch.mesh.faces).astype(np.int64),
        skin_joints=skin_j,
        skin_weights=skin_w,
        dropped_weight_max=dropped,
        texcoords=texcoords if len(texcoords) else None,
        texcoord_faces=texcoord_faces if len(texcoords) else None,
        asset_sha256="sha256:" + (expected_sha256.removeprefix("sha256:") if expected_sha256 else file_sha256(asset_path)),
    )


def scales_from_scale_params(scale_params: np.ndarray, scale_mean: np.ndarray, scale_comps: np.ndarray) -> np.ndarray:
    """HMR 출력 scale_params(28) → MHR 체형 파라미터(68) = scale_mean + scale_params·scale_comps.

    scale_mean (68)·scale_comps (28, 68) 은 MHR 자산이 아니라 SAM 3D Body 모델의 `head_pose.scale_mean`·`head_pose.scale_comps` 다
    (Fast-SAM-3D-Body `mhr_head.py`). 모델이 내는 `mhr_model_params[136:204]` 가 있으면 그것을 바로 쓴다.
    """
    return np.asarray(scale_mean, dtype=np.float64) + np.asarray(scale_params, dtype=np.float64) @ np.asarray(
        scale_comps, dtype=np.float64
    )


def model_params_vector(rig: MhrRig, scales: np.ndarray, body_pose: np.ndarray | None = None) -> np.ndarray:
    """파라미터 변환 입력 벡터(전역 0, 몸 자세, 체형, 정체 자리 0). 몸 자세는 뼈 길이를 바꾸는 *_flexible 값 때문에 받는다."""
    mp = np.zeros(rig.parameter_transform.shape[1])
    if body_pose is not None:
        mp[6 : 6 + MHR_BODY_POSE_COUNT] = np.asarray(body_pose, dtype=np.float64)[:MHR_BODY_POSE_COUNT]
    mp[6 + MHR_BODY_POSE_COUNT : MHR_MODEL_PARAM_COUNT] = np.asarray(scales, dtype=np.float64)
    return mp


def template_inverse_bind_matrices(rig: MhrRig) -> np.ndarray:
    """템플릿 역바인드 (J, 4, 4), 길이 m."""
    return skel_state_to_matrix(rig.inverse_bind_pose, CM_TO_M)


def bake_person_rig(
    rig: MhrRig,
    shape_params: np.ndarray,
    scales: np.ndarray,
    body_pose: np.ndarray | None = None,
) -> PersonRig:
    """사람 한 명의 체형을 구운 리그를 만든다.

    - shape_params (45): 정체 blendshape 계수 → 정점.
    - scales (68): MHR 체형 파라미터(`scales_from_scale_params` 또는 모델 `mhr_model_params[136:204]`) → 노드 이동·크기·무릎 휨.
    - body_pose (130, 선택): 몸 자세 파라미터. 회전은 스트림이 싣으므로 여기서는 뼈 길이를 바꾸는 *_flexible 이동 기여만 굽는다.
      None 이면 그 기여를 0 으로 둔다. 체형을 고정할 때(-227, 30 프레임) 같은 창의 평균을 넣는다.

    노드 휴지 회전 = 사전 회전 · 오일러(체형 파라미터만의 회전 기여). 자세 회전은 스트림이 노드 회전을 통째로 바꾼다.
    """
    J = rig.joint_count
    shape_params = np.asarray(shape_params, dtype=np.float64)
    rest_cm = rig.base_shape_cm + np.einsum("svd,s->vd", rig.shape_vectors_cm, shape_params)

    scale_only = model_params_vector(rig, scales, None)
    with_lengths = model_params_vector(rig, scales, body_pose)
    jp_scale = (rig.parameter_transform @ scale_only).reshape(J, 7)
    jp_len = (rig.parameter_transform @ with_lengths).reshape(J, 7)

    translations_cm = jp_len[:, 0:3] + rig.joint_offsets_cm
    local_R = quat_xyzw_to_matrix(rig.joint_prerotations) @ euler_xyz_to_matrix(jp_scale[:, 3:6])
    local_scales = np.exp2(jp_len[:, 6])
    return PersonRig(
        rig=rig,
        rest_vertices_m=rest_cm * CM_TO_M,
        local_translations_m=translations_cm * CM_TO_M,
        local_rotations=matrix_to_quat_xyzw(local_R),
        local_scales=local_scales,
        inverse_bind_matrices=template_inverse_bind_matrices(rig),
    )


def split_model_params(model_params: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """모델 출력 `mhr_model_params` (204) → (몸 자세 130, 체형 scale 68)."""
    mp = np.asarray(model_params, dtype=np.float64)
    if mp.shape != (MHR_MODEL_PARAM_COUNT,):
        raise ValueError(f"mhr_model_params 길이 {mp.shape} != ({MHR_MODEL_PARAM_COUNT},)")
    return mp[6 : 6 + MHR_BODY_POSE_COUNT], mp[6 + MHR_BODY_POSE_COUNT :]


def bake_person_rig_from_model_params(rig: MhrRig, shape_params: np.ndarray, model_params: np.ndarray) -> PersonRig:
    """`bake_person_rig` 를 모델 출력 `mhr_model_params` (204, 손 IK 반영 뒤 값) 하나로 부른다."""
    body_pose, scales = split_model_params(model_params)
    return bake_person_rig(rig, shape_params, scales, body_pose)
