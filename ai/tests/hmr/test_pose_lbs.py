"""HMR → 계약 포즈 변환과 glTF LBS 일치 시험 (S15P31A101-88, 합성 리그).

- local ↔ global 회전 왕복, 축각 ↔ 행렬 왕복(π 근처 포함)
- 내보낸 .glb(파일에서 다시 읽은 노드 TRS·역바인드·가중치) + 변환한 포즈(소수 넷째 자리)로 돌린 LBS 가
  참조 MHR 식(pymomentum 관절 상태 계산)과 pelvis 를 맞춘 뒤 같은지
- 포즈 묶음이 계약 스키마(avatar_pose_batch)와 스키마 밖 규칙(회전 길이 ≤ π, 관절 0 = 0)을 지키는지, 손가락 null
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from simai_ai.hmr.gltf_export import accessor_array, build_skinned_glb, read_glb
from simai_ai.hmr.lbs import (
    global_matrices,
    node_matrices,
    posed_rotations,
    skin_vertices,
)
from simai_ai.hmr.pose import (
    CAMERA_FROM_MHR,
    finger_joint_indices,
    global_rotations_from_local,
    local_rotations_from_global,
    mhr_pose,
    pose_from_hmr,
    pose_from_model_params,
    world_root_rotation,
)
from simai_ai.hmr.rig import bake_person_rig_from_model_params
from simai_ai.hmr.rotation import matrix_to_rotvec, rotvec_to_matrix
from simai_ai.providers import contract

from .conftest import (
    joint_params,
    momentum_global_states,
    momentum_local,
    random_model_params,
    reference_vertices_cm,
)


def _random_rotations(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=(n, 3))
    v *= (rng.uniform(0, math.pi, n) / np.linalg.norm(v, axis=1))[:, None]
    return rotvec_to_matrix(v)


def test_rotvec_matrix_roundtrip_including_near_pi():
    rng = np.random.default_rng(3)
    axes = rng.normal(size=(50, 3))
    axes /= np.linalg.norm(axes, axis=1, keepdims=True)
    angles = np.concatenate([rng.uniform(0, math.pi, 46), [0.0, 1e-9, math.pi - 1e-7, math.pi]])
    R = rotvec_to_matrix(axes * angles[:, None])
    back = matrix_to_rotvec(R)
    assert (np.linalg.norm(back, axis=1) <= math.pi + 1e-9).all()
    np.testing.assert_allclose(rotvec_to_matrix(back), R, atol=1e-9)


def test_local_global_roundtrip():
    parents = np.array([-1, 0, 1, 1, 3, 0])
    G = _random_rotations(len(parents))
    L = local_rotations_from_global(G, parents)
    np.testing.assert_allclose(global_rotations_from_local(L, parents), G, atol=1e-12)


def _glb_values(data: bytes) -> dict:
    gltf, binary = read_glb(data)
    skin = gltf["skins"][0]
    attrs = gltf["meshes"][0]["primitives"][0]["attributes"]
    nodes = [gltf["nodes"][j] for j in skin["joints"]]
    parents = np.full(len(nodes), -1)
    for j, n in enumerate(nodes):
        for c in n.get("children", []):
            parents[c] = j
    ibm = accessor_array(gltf, binary, skin["inverseBindMatrices"]).reshape(-1, 4, 4)
    return {
        "parents": parents,
        "t": np.array([n["translation"] for n in nodes]),
        "q": np.array([n["rotation"] for n in nodes]),
        "s": np.array([n.get("scale", [1.0])[0] for n in nodes]),
        "ibm": np.transpose(ibm, (0, 2, 1)).astype(np.float64),
        "pos": accessor_array(gltf, binary, attrs["POSITION"]).astype(np.float64),
        "jnt": accessor_array(gltf, binary, attrs["JOINTS_0"]).astype(np.int64),
        "wgt": accessor_array(gltf, binary, attrs["WEIGHTS_0"]).astype(np.float64),
        "src": accessor_array(gltf, binary, attrs["_MHR_VERTEX_INDEX"]).astype(np.int64),
    }


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_exported_glb_lbs_matches_reference(rig, seed):
    """HMR 카메라 정점(F · 참조 MHR 정점) = glb + 계약 포즈 LBS (pelvis 맞춤 뒤, 반올림 포함 0.5 mm 안)."""
    mp = random_model_params(seed)
    shape = np.array([0.4, -0.7])
    ref_cm, ref_joints_cm = reference_vertices_cm(rig, shape, mp)
    pred_vertices = (CAMERA_FROM_MHR @ ref_cm.T).T * 0.01
    pred_pelvis = CAMERA_FROM_MHR @ ref_joints_cm[1] * 0.01

    glb = build_skinned_glb(bake_person_rig_from_model_params(rig, shape, mp))
    g = _glb_values(glb.data)
    pose = pose_from_model_params(rig, mp, hands_estimated=True)
    assert pose.joint_rotations[0] == [0.0, 0.0, 0.0] and pose.joint_rotations[1] == [0.0, 0.0, 0.0]
    R = posed_rotations(g["q"], pose.joint_rotations)
    G = global_matrices(g["parents"], node_matrices(g["t"], R, g["s"]), pose.camera_from_rig)
    V = skin_vertices(g["pos"], g["jnt"], g["wgt"], G, g["ibm"]) + (pred_pelvis - G[1, :3, 3])
    err = np.linalg.norm(V - pred_vertices[g["src"]], axis=1)
    assert err.max() < 5e-4


def test_pose_from_global_rots_equals_model_params_path(rig):
    mp = random_model_params(4)
    t, q, s = momentum_local(rig, joint_params(rig, mp))
    _, Rg, _ = momentum_global_states(rig.parents, t, q, s)
    a = pose_from_hmr(Rg, rig.parents, hands_estimated=True, decimals=None)
    b = pose_from_model_params(rig, mp, hands_estimated=True, decimals=None)
    np.testing.assert_allclose(a.camera_from_rig, b.camera_from_rig, atol=1e-12)
    np.testing.assert_allclose(np.array(a.joint_rotations), np.array(b.joint_rotations), atol=1e-9)
    np.testing.assert_allclose(a.camera_from_rig, CAMERA_FROM_MHR @ Rg[1], atol=1e-12)


def _mhr_like_parents() -> np.ndarray:
    """127 관절, 관절 1 이 관절 0 의 유일한 자식인 나무(실자산과 같은 손가락 번호를 쓰기 위한 모양)."""
    return np.array([-1, 0] + [max(1, j - 1 - (j % 3 == 0)) for j in range(2, 127)])


def test_fingers_null_in_body_mode_and_pelvis_chain_zero():
    parents = _mhr_like_parents()
    G = _random_rotations(127, seed=5)
    body = pose_from_hmr(G, parents, hands_estimated=False)
    full = pose_from_hmr(G, parents, hands_estimated=True)
    fingers = set(finger_joint_indices())
    assert fingers == set(range(43, 65)) | set(range(79, 101))
    assert all((body.joint_rotations[j] is None) == (j in fingers) for j in range(127))
    assert all(r is not None for r in full.joint_rotations)
    assert body.joint_rotations[0] == [0.0, 0.0, 0.0] == body.joint_rotations[1]
    with pytest.raises(ValueError):
        pose_from_hmr(G, np.array([-1, 0, 0]), hands_estimated=True)  # pelvis 밖의 관절이 루트에 직접 달림
    with pytest.raises(ValueError):
        pose_from_hmr(G[:10], parents, hands_estimated=True)


def test_world_root_rotation_upright_person_is_rx_half_pi():
    """수평 카메라 앞에 똑바로 선 사람(pelvis global = 단위): root_rotation = Rx(+π/2) (설계 문서 '똑바로 선 사람')."""
    level_world_to_camera = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
    root = world_root_rotation(CAMERA_FROM_MHR @ np.eye(3), level_world_to_camera)
    assert root == [round(math.pi / 2, 4), 0.0, 0.0]


def _batch(pose: dict) -> dict:
    return {
        "schema_version": "1.0.0",
        "msg_type": "avatar_pose_batch",
        "msg_id": "7f1c2a10-4b6e-4c1d-9a2b-000000000088",
        "sent_at": 1761460000250,
        "source_id": "worker-01",
        "payload": {
            "stream_epoch": 1761459940000,
            "batch_sequence": 0,
            "calibration_version": "cal-7",
            "model_version": "Fast-SAM-3D-Body@d72aa36 + sam-3d-body-dinov3",
            "input_provenance": {
                "frames": {"source": "LIVE", "camera_ids": ["cam1"]},
                "tracks": {"source": "TIER0_LIVE", "provider_version": "tier0-0.1.0"},
                "is_upper_bound": False,
            },
            "dropped_poses": 0,
            "poses": [{
                "world_id": "w-0001", "identity_revision": 0, "capture_time_ms": 1761460000000,
                "appearance_version": 0, "joint_source": "inferred", "pose": pose,
            }],
        },
    }


@pytest.mark.parametrize("hands_estimated", [False, True])
def test_pose_batch_passes_contract(hands_estimated):
    parents = _mhr_like_parents()
    G = _random_rotations(127, seed=7)
    hp = pose_from_hmr(G, parents, hands_estimated=hands_estimated)
    R_cal = _random_rotations(1, seed=8)[0]
    pose = mhr_pose(world_root_rotation(hp.camera_from_rig, R_cal), hp.joint_rotations)
    assert contract.schema_errors(_batch(pose), "avatar_pose_batch") == []
    rots = [pose["root_rotation"], *pose["joint_rotations"]]
    assert len(pose["joint_rotations"]) == 127 and pose["joint_rotations"][0] == [0.0, 0.0, 0.0]
    assert all(math.hypot(*r) <= math.pi + 1e-4 for r in rots if r is not None)
    # 반올림한 값으로 되돌린 회전이 원래 회전과 1e-3 rad 안
    np.testing.assert_allclose(
        rotvec_to_matrix(np.array(pose["root_rotation"])), R_cal.T @ hp.camera_from_rig, atol=1e-3
    )
