"""실제 MHR 자산 시험 (S15P31A101-88). 환경 변수 SIMAI_MHR_ASSET(mhr_model.pt 경로)이 있고 torch 가 있을 때만 돈다.

자산은 SAM License(재배포 금지)라 저장소·CI 에 없다. 노트북에서:
    SIMAI_MHR_ASSET=<.../sam-3d-body-dinov3/assets/mhr_model.pt> <torch 가 있는 python> -m pytest tests/hmr/test_mhr_asset.py
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from simai_ai.hmr.gltf_export import build_skinned_glb, read_glb
from simai_ai.hmr.lbs import global_matrices, node_matrices
from simai_ai.hmr.pose import finger_joint_indices
from simai_ai.hmr.rig import (
    MHR_FACE_COUNT,
    MHR_JOINT_COUNT,
    MHR_VERTEX_COUNT,
    bake_person_rig,
    joint_parents_sha256,
    load_mhr_rig,
    template_inverse_bind_matrices,
)
from simai_ai.hmr.rotation import euler_xyz_to_matrix, quat_xyzw_to_matrix
from simai_ai.providers.testing import load_example

ASSET = os.environ.get("SIMAI_MHR_ASSET")
pytestmark = pytest.mark.skipif(not ASSET, reason="SIMAI_MHR_ASSET 가 없다(자산은 저장소 밖)")


@pytest.fixture(scope="module")
def torch_model():
    torch = pytest.importorskip("torch")
    return torch, torch.jit.load(ASSET, map_location="cpu")


@pytest.fixture(scope="module")
def rig():
    pytest.importorskip("torch")
    return load_mhr_rig(ASSET)


def test_asset_shape_matches_contract(rig):
    assert rig.joint_count == MHR_JOINT_COUNT and rig.vertex_count == MHR_VERTEX_COUNT and len(rig.faces) == MHR_FACE_COUNT
    example = load_example("avatar_asset.json")["payload"]["skeleton"]
    assert joint_parents_sha256(rig.parents) == example["joint_parents_sha256"]
    assert rig.joint_names[0] == "body_world" and rig.joint_names[1] == "root"
    assert all(rig.parents[j] < j for j in range(rig.joint_count))
    assert [j for j in range(rig.joint_count) if rig.parents[j] == 0] == [1]  # pelvis 만 바닥 원점에 달림
    names = [rig.joint_names[j] for j in finger_joint_indices()]
    assert names[0] == "r_pinky0" and names[21] == "r_thumb_null" and names[22] == "l_pinky0" and names[-1] == "l_thumb_null"
    assert rig.dropped_weight_max == 0.0  # 자산 영향이 정점당 최대 4개
    # 몸 방향을 root_rotation 으로 옮기는 변환(pose.py)은 관절 0 에 스킨 가중치가 없어야 정확하다
    assert not (rig.skin_joints[rig.skin_weights > 0] == 0).any()


def test_euler_and_inverse_bind_follow_asset(rig, torch_model):
    torch, model = torch_model
    sk = model.character_torch.skeleton
    rng = np.random.default_rng(0)
    jp = np.zeros((rig.joint_count, 7))
    jp[:, 3:6] = rng.uniform(-1, 1, (rig.joint_count, 3))
    state = sk.joint_parameters_to_local_skeleton_state(torch.tensor(jp.reshape(1, -1), dtype=torch.float32)).numpy()[0]
    mine = quat_xyzw_to_matrix(rig.joint_prerotations) @ euler_xyz_to_matrix(jp[:, 3:6])
    np.testing.assert_allclose(quat_xyzw_to_matrix(state[:, 3:7]), mine, atol=1e-5)
    rest = bake_person_rig(rig, np.zeros(45), np.zeros(68), None)
    G = global_matrices(rig.parents, node_matrices(rest.local_translations_m, rest.local_rotations, rest.local_scales))
    np.testing.assert_allclose(G @ template_inverse_bind_matrices(rig), np.tile(np.eye(4), (rig.joint_count, 1, 1)), atol=1e-5)


def test_glb_from_asset(rig):
    glb = build_skinned_glb(bake_person_rig(rig, np.zeros(45), np.zeros(68), None))
    gltf, _ = read_glb(glb.data)
    assert glb.mhr_vertex_count == MHR_VERTEX_COUNT and glb.face_count == MHR_FACE_COUNT
    assert glb.vertex_count >= len(rig.texcoords)  # UV 이음매 분할
    assert len(gltf["skins"][0]["joints"]) == MHR_JOINT_COUNT
    assert glb.size_bytes < 4 * 1024 * 1024
