"""사람별 MHR 리그 → 스키닝 glTF 2.0 바이너리(.glb) 쓰기 (S15P31A101-88, docs/design/mhr-skinned-gltf.md).

표준 라이브러리 + numpy 로 직접 쓴다(새 의존성 없음).
- `skin.joints[i]` = MHR 관절 i 노드(노드 인덱스도 i), 노드 계층 = MHR 부모 배열, 노드 이름 = 자산 관절 이름.
- 단위 m, Y-up(계약 `GLTF_Y_UP`). 노드 휴지 TRS 는 그 사람 체형, 역바인드는 템플릿(`rig.py` 설명).
- 정점 속성: POSITION·NORMAL·JOINTS_0(UNSIGNED_BYTE)·WEIGHTS_0(FLOAT, 합 1)·TEXCOORD_0(자산 UV, 텍스처 없음)·
  `_MHR_VERTEX_INDEX`(FLOAT, 분할 전 MHR 정점 번호). UV 이음매 때문에 정점을 (위치, UV) 쌍으로 나눈다.
- 텍스처는 넣지 않는다(외형은 -89). 얼굴도 메시만 있고 생성형 복원은 없다(AGENTS 규칙 8).
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass

import numpy as np

from simai_ai.hmr.rig import PersonRig, joint_parents_sha256

GLB_MAGIC = 0x46546C67  # "glTF"
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942
FLOAT, UNSIGNED_BYTE, UNSIGNED_SHORT, UNSIGNED_INT = 5126, 5121, 5123, 5125
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963
_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_DTYPES = {FLOAT: np.float32, UNSIGNED_BYTE: np.uint8, UNSIGNED_SHORT: np.uint16, UNSIGNED_INT: np.uint32}
GENERATOR = "simai_ai.hmr.gltf_export (S15P31A101-88)"


@dataclass(frozen=True)
class SkinnedGlb:
    data: bytes
    sha256: str  # "sha256:<hex>" (계약 avatar_asset.mesh.sha256 형식)
    vertex_count: int  # glTF POSITION 개수(UV 이음매 분할 뒤)
    face_count: int
    mhr_vertex_count: int  # 분할 전 MHR 정점 수(18,439)
    joint_count: int
    joint_parents_sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def vertex_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """면적 가중 정점 법선(단위 길이). 면이 없는 정점은 +Y."""
    v = np.asarray(vertices, dtype=np.float64)
    tri = v[faces]
    fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n = np.zeros_like(v)
    for k in range(3):
        np.add.at(n, faces[:, k], fn)
    length = np.linalg.norm(n, axis=1)
    n[length < 1e-12] = [0.0, 1.0, 0.0]
    length[length < 1e-12] = 1.0
    return n / length[:, None]


def split_uv_seams(faces: np.ndarray, texcoord_faces: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(위치 번호, UV 번호) 쌍마다 정점 하나. 반환: (원본 위치 번호 (N,), UV 번호 (N,), 새 면 (F, 3))."""
    pos = np.asarray(faces, dtype=np.int64).reshape(-1)
    uv = np.asarray(texcoord_faces, dtype=np.int64).reshape(-1)
    key = pos * (int(uv.max()) + 1) + uv
    uniq, inverse = np.unique(key, return_inverse=True)
    width = int(uv.max()) + 1
    return uniq // width, uniq % width, inverse.reshape(-1, 3)


def _normalized_weights_f32(weights: np.ndarray) -> np.ndarray:
    """float32 로 바꾼 뒤에도 합이 1 이 되게 남는 몫을 가장 큰 가중치에 더한다."""
    w = (weights / weights.sum(axis=1, keepdims=True)).astype(np.float32)
    rows = np.arange(len(w))
    top = np.argmax(w, axis=1)
    w[rows, top] += (np.float32(1.0) - w.sum(axis=1, dtype=np.float32)).astype(np.float32)
    return w


class _BinWriter:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.offset = 0
        self.buffer_views: list[dict] = []
        self.accessors: list[dict] = []

    def add(self, array: np.ndarray, component_type: int, kind: str, target: int | None, minmax: bool = False) -> int:
        data = np.ascontiguousarray(array, dtype=_DTYPES[component_type])
        raw = data.tobytes()
        view = {"buffer": 0, "byteOffset": self.offset, "byteLength": len(raw)}
        if target is not None:
            view["target"] = target
        self.buffer_views.append(view)
        pad = (-len(raw)) % 4
        self.chunks.append(raw + b"\x00" * pad)
        self.offset += len(raw) + pad
        count = data.size if kind == "SCALAR" else data.shape[0]
        acc = {"bufferView": len(self.buffer_views) - 1, "componentType": component_type, "count": int(count), "type": kind}
        if minmax:
            flat = data.reshape(count, _COMPONENTS[kind])
            cast = float if component_type == FLOAT else int
            acc["min"] = [cast(x) for x in flat.min(axis=0)]
            acc["max"] = [cast(x) for x in flat.max(axis=0)]
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def blob(self) -> bytes:
        return b"".join(self.chunks)


def build_skinned_glb(person: PersonRig, *, include_uv: bool = True, name: str = "mhr_avatar") -> SkinnedGlb:
    """사람별 리그를 .glb 바이트로 쓴다. 같은 입력이면 같은 바이트(sha256)가 나온다."""
    rig = person.rig
    J = rig.joint_count
    if J > 255:
        raise ValueError("JOINTS_0 를 UNSIGNED_BYTE 로 쓰므로 관절은 255개 이하여야 한다")
    rest = person.rest_vertices_m
    normals_src = vertex_normals(rest, rig.faces)
    if include_uv and rig.texcoords is not None and rig.texcoord_faces is not None:
        src, uv_idx, faces = split_uv_seams(rig.faces, rig.texcoord_faces)
        # MHR UV 는 아래 왼쪽 원점(FBX·OBJ 규약)으로 보고 glTF(위 왼쪽 원점)로 v 를 뒤집는다(텍스처가 생기면 -89 가 확인)
        uv = rig.texcoords[uv_idx].copy()
        uv[:, 1] = 1.0 - uv[:, 1]
    else:
        src, faces, uv = np.arange(len(rest)), np.asarray(rig.faces, dtype=np.int64), None
    positions = rest[src]
    normals = normals_src[src]
    joints = rig.skin_joints[src]
    weights = _normalized_weights_f32(rig.skin_weights[src])
    joints = np.where(weights > 0, joints, 0)  # 가중치 0 칸은 관절 0(glTF 검증기 ACCESSOR_JOINTS_USED_ZERO_WEIGHT)

    w = _BinWriter()
    attributes = {
        "POSITION": w.add(positions, FLOAT, "VEC3", ARRAY_BUFFER, minmax=True),
        "NORMAL": w.add(normals, FLOAT, "VEC3", ARRAY_BUFFER),
        "JOINTS_0": w.add(joints, UNSIGNED_BYTE, "VEC4", ARRAY_BUFFER),
        "WEIGHTS_0": w.add(weights, FLOAT, "VEC4", ARRAY_BUFFER),
    }
    if uv is not None:
        attributes["TEXCOORD_0"] = w.add(uv, FLOAT, "VEC2", ARRAY_BUFFER, minmax=True)
    attributes["_MHR_VERTEX_INDEX"] = w.add(src.astype(np.float32), FLOAT, "SCALAR", ARRAY_BUFFER, minmax=True)
    index_type = UNSIGNED_SHORT if len(positions) <= 65535 else UNSIGNED_INT
    indices = w.add(faces.reshape(-1), index_type, "SCALAR", ELEMENT_ARRAY_BUFFER, minmax=True)
    # 역바인드: glTF 행렬은 열 우선
    ibm = w.add(np.transpose(person.inverse_bind_matrices, (0, 2, 1)).reshape(J, 16), FLOAT, "MAT4", None)

    children: dict[int, list[int]] = {}
    for j, p in enumerate(rig.parents):
        if p >= 0:
            children.setdefault(int(p), []).append(j)
    nodes = []
    for j in range(J):
        q = np.asarray(person.local_rotations[j], dtype=np.float64)
        q = q / np.linalg.norm(q)
        node = {
            "name": rig.joint_names[j] if j < len(rig.joint_names) else f"joint_{j}",
            "translation": [float(x) for x in person.local_translations_m[j]],
            "rotation": [float(x) for x in q],
        }
        s = float(person.local_scales[j])
        if abs(s - 1.0) > 1e-9:
            node["scale"] = [s, s, s]
        if j in children:
            node["children"] = children[j]
        nodes.append(node)
    mesh_node = len(nodes)
    nodes.append({"name": f"{name}_mesh", "mesh": 0, "skin": 0})
    roots = [j for j, p in enumerate(rig.parents) if p < 0]

    parents_sha = joint_parents_sha256(rig.parents)
    gltf = {
        "asset": {"version": "2.0", "generator": GENERATOR},
        "scene": 0,
        "scenes": [{"name": name, "nodes": [*roots, mesh_node]}],
        "nodes": nodes,
        "meshes": [{"name": name, "primitives": [{"attributes": attributes, "indices": indices, "material": 0, "mode": 4}]}],
        "materials": [
            {
                "name": "default_unobserved",
                "pbrMetallicRoughness": {"baseColorFactor": [0.7, 0.7, 0.72, 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.85},
            }
        ],
        "skins": [{"name": "mhr", "inverseBindMatrices": ibm, "joints": list(range(J)), "skeleton": roots[0]}],
        "accessors": w.accessors,
        "bufferViews": w.buffer_views,
        "buffers": [{"byteLength": w.offset}],
        "extras": {
            "simai": {
                "rig": "MHR",
                "joint_count": J,
                "joint_parents_sha256": parents_sha,
                "pelvis_joint_index": 1,
                "rig_source": rig.rig_source,
                "axis_convention": "GLTF_Y_UP",
                "unit": "m",
                "mhr_vertex_count": rig.vertex_count,
                "inverse_bind": "MHR template (node rest TRS carries the person's bone lengths)",
                "texture": None,
            }
        },
    }
    data = _pack_glb(gltf, w.blob())
    return SkinnedGlb(
        data=data,
        sha256="sha256:" + hashlib.sha256(data).hexdigest(),
        vertex_count=len(positions),
        face_count=len(faces),
        mhr_vertex_count=rig.vertex_count,
        joint_count=J,
        joint_parents_sha256=parents_sha,
    )


def _pack_glb(gltf: dict, binary: bytes) -> bytes:
    js = json.dumps(gltf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    js += b" " * ((-len(js)) % 4)
    binary += b"\x00" * ((-len(binary)) % 4)
    total = 12 + 8 + len(js) + 8 + len(binary)
    return b"".join(
        [
            struct.pack("<III", GLB_MAGIC, 2, total),
            struct.pack("<II", len(js), CHUNK_JSON),
            js,
            struct.pack("<II", len(binary), CHUNK_BIN),
            binary,
        ]
    )


def read_glb(data: bytes) -> tuple[dict, bytes]:
    """.glb → (glTF JSON, BIN 청크). 머리말·청크 길이·정렬을 확인한다."""
    magic, version, total = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC or version != 2 or total != len(data):
        raise ValueError("GLB 머리말이 맞지 않는다")
    json_len, json_type = struct.unpack_from("<II", data, 12)
    if json_type != CHUNK_JSON or json_len % 4:
        raise ValueError("JSON 청크가 맞지 않는다")
    gltf = json.loads(data[20 : 20 + json_len].decode("utf-8"))
    off = 20 + json_len
    bin_len, bin_type = struct.unpack_from("<II", data, off)
    if bin_type != CHUNK_BIN or bin_len % 4 or off + 8 + bin_len != total:
        raise ValueError("BIN 청크가 맞지 않는다")
    return gltf, data[off + 8 : off + 8 + bin_len]


def accessor_array(gltf: dict, binary: bytes, index: int) -> np.ndarray:
    """accessor 를 numpy 배열로 읽는다(byteStride 없는 단순 배치만)."""
    acc = gltf["accessors"][index]
    view = gltf["bufferViews"][acc["bufferView"]]
    n = _COMPONENTS[acc["type"]]
    dtype = _DTYPES[acc["componentType"]]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    arr = np.frombuffer(binary, dtype=dtype, count=acc["count"] * n, offset=start)
    return arr.reshape(acc["count"], n) if n > 1 else arr
