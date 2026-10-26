"""MHR → 스키닝 glTF 내보내기 구조 시험 (S15P31A101-88, 합성 리그).

glTF 검증기 없이 확인하는 것: GLB 머리말·청크 정렬, bufferView 4바이트 정렬, accessor min/max, 가중치 합 1·관절 범위,
skin.joints[i] = 관절 i 노드, 노드 계층 = 부모 배열, 역바인드 마지막 행, 법선 단위 길이, UV 이음매 분할의 원본 정점 번호.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from simai_ai.hmr.gltf_export import (
    FLOAT,
    UNSIGNED_BYTE,
    UNSIGNED_SHORT,
    accessor_array,
    build_skinned_glb,
    read_glb,
    split_uv_seams,
)
from simai_ai.hmr.lbs import global_matrices, node_matrices
from simai_ai.hmr.rig import bake_person_rig, joint_parents_sha256, reduce_skin_weights

from .conftest import make_rig, random_model_params

_SIZES = {FLOAT: 4, UNSIGNED_BYTE: 1, UNSIGNED_SHORT: 2, 5125: 4}


@pytest.fixture
def person(rig):
    mp = random_model_params()
    return bake_person_rig(rig, np.array([0.3, -0.2]), mp[136:], mp[6:136])


@pytest.fixture
def exported(person):
    glb = build_skinned_glb(person)
    gltf, binary = read_glb(glb.data)
    return glb, gltf, binary


def test_reduce_skin_weights_keeps_top4_normalized():
    vi = np.array([0, 0, 0, 0, 0, 1])
    ji = np.array([0, 1, 2, 3, 4, 2])
    wi = np.array([0.1, 0.4, 0.2, 0.2, 0.1, 1.0])
    j, w, dropped = reduce_skin_weights(vi, ji, wi, 2)
    assert j[0, 0] == 1 and set(j[0]) <= {0, 1, 2, 3, 4} and len(set(j[0])) == 4
    np.testing.assert_allclose(w.sum(axis=1), 1.0)
    assert dropped == pytest.approx(0.1)
    with pytest.raises(ValueError):
        reduce_skin_weights(vi, ji, wi, 3)  # 정점 2 는 가중치가 없다(지어내지 않음)


def test_glb_container_and_alignment(exported):
    glb, gltf, binary = exported
    assert glb.data[:4] == b"glTF" and len(glb.data) % 4 == 0
    assert gltf["asset"]["version"] == "2.0"
    assert gltf["buffers"][0]["byteLength"] <= len(binary)
    for view in gltf["bufferViews"]:
        assert view["byteOffset"] % 4 == 0
        assert view["byteOffset"] + view["byteLength"] <= gltf["buffers"][0]["byteLength"]
    for acc in gltf["accessors"]:
        assert acc.get("byteOffset", 0) % _SIZES[acc["componentType"]] == 0
    assert glb.sha256 == "sha256:" + hashlib.sha256(glb.data).hexdigest()
    assert build_skinned_glb(_same_person()).sha256 == glb.sha256  # 같은 입력이면 같은 바이트


def _same_person():
    rig = make_rig()
    mp = random_model_params()
    return bake_person_rig(rig, np.array([0.3, -0.2]), mp[136:], mp[6:136])


def test_accessor_minmax_and_attributes(exported, person):
    glb, gltf, binary = exported
    prim = gltf["meshes"][0]["primitives"][0]
    attrs = prim["attributes"]
    for name in ("POSITION", "TEXCOORD_0", "_MHR_VERTEX_INDEX"):
        acc = gltf["accessors"][attrs[name]]
        data = accessor_array(gltf, binary, attrs[name]).reshape(acc["count"], -1)
        assert acc["min"] == [float(x) for x in data.min(axis=0)]
        assert acc["max"] == [float(x) for x in data.max(axis=0)]
    pos = accessor_array(gltf, binary, attrs["POSITION"])
    src = accessor_array(gltf, binary, attrs["_MHR_VERTEX_INDEX"]).astype(int)
    np.testing.assert_allclose(pos, person.rest_vertices_m[src], atol=1e-6)
    assert sorted(set(src)) == list(range(person.rig.vertex_count))  # 모든 원본 정점이 남는다
    assert glb.vertex_count == len(pos) > person.rig.vertex_count  # UV 이음매로 늘었다
    normals = accessor_array(gltf, binary, attrs["NORMAL"])
    np.testing.assert_allclose(np.linalg.norm(normals, axis=1), 1.0, atol=1e-6)
    weights = accessor_array(gltf, binary, attrs["WEIGHTS_0"])
    joints = accessor_array(gltf, binary, attrs["JOINTS_0"])
    assert gltf["accessors"][attrs["JOINTS_0"]]["componentType"] == UNSIGNED_BYTE
    np.testing.assert_allclose(weights.sum(axis=1, dtype=np.float64), 1.0, atol=1e-6)
    assert (weights >= 0).all() and joints.max() < person.rig.joint_count
    assert (joints[weights == 0] == 0).all()  # 가중치 0 칸은 관절 0(glTF 검증기 경고 없음)
    for jv, wv in zip(joints, weights):  # 가중치가 있는 관절은 겹치지 않는다
        used = jv[wv > 0]
        assert len(set(used)) == len(used)
    idx = accessor_array(gltf, binary, prim["indices"])
    assert idx.max() < len(pos) and len(idx) == 3 * glb.face_count
    uv = accessor_array(gltf, binary, attrs["TEXCOORD_0"])
    assert ((uv >= 0) & (uv <= 1)).all()


def test_skin_joints_nodes_follow_mhr_parents(exported, person):
    _, gltf, binary = exported
    rig = person.rig
    skin = gltf["skins"][0]
    assert skin["joints"] == list(range(rig.joint_count))
    parents = np.full(rig.joint_count, -1)
    for j in range(rig.joint_count):
        for c in gltf["nodes"][j].get("children", []):
            parents[c] = j
    np.testing.assert_array_equal(parents, rig.parents)
    assert [gltf["nodes"][j]["name"] for j in range(rig.joint_count)] == list(rig.joint_names)
    mesh_node = gltf["scenes"][0]["nodes"][-1]
    assert gltf["nodes"][mesh_node]["skin"] == 0 and all(mesh_node not in n.get("children", []) for n in gltf["nodes"])
    ibm = accessor_array(gltf, binary, skin["inverseBindMatrices"]).reshape(-1, 4, 4)
    np.testing.assert_allclose(ibm[:, :, 3], np.tile([0, 0, 0, 1], (rig.joint_count, 1)), atol=1e-7)  # 열 우선: 마지막 행
    extras = gltf["extras"]["simai"]
    assert extras["joint_parents_sha256"] == joint_parents_sha256(rig.parents)
    assert extras["axis_convention"] == "GLTF_Y_UP" and extras["unit"] == "m"
    assert gltf["nodes"][4]["scale"][0] != 1.0  # 체형 크기 파라미터가 노드 크기로 갔다


def test_template_inverse_bind_matches_rest_when_no_identity_params(rig):
    """체형·flexible 이 0 이면 노드 휴지 global · 역바인드 = 단위(템플릿 바인드)."""
    person = bake_person_rig(rig, np.zeros(2), np.zeros(68), None)
    G = global_matrices(rig.parents, node_matrices(person.local_translations_m, person.local_rotations, person.local_scales))
    np.testing.assert_allclose(G @ person.inverse_bind_matrices, np.tile(np.eye(4), (rig.joint_count, 1, 1)), atol=1e-9)


def test_split_uv_seams_unique_pairs():
    faces = np.array([[0, 1, 2], [2, 1, 3]])
    tex = np.array([[0, 1, 2], [4, 1, 3]])  # 정점 2 가 UV 2·4 로 갈라진다
    src, uv, new_faces = split_uv_seams(faces, tex)
    assert len(src) == 5 and sorted(src.tolist()) == [0, 1, 2, 2, 3]
    np.testing.assert_array_equal(src[new_faces], faces)
    np.testing.assert_array_equal(uv[new_faces], tex)


def test_without_uv_keeps_mhr_vertex_count(person):
    glb = build_skinned_glb(person, include_uv=False)
    gltf, _ = read_glb(glb.data)
    assert glb.vertex_count == person.rig.vertex_count
    assert "TEXCOORD_0" not in gltf["meshes"][0]["primitives"][0]["attributes"]
