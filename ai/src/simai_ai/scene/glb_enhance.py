"""3D 맵(GLB) 시각 보강: 바닥 평면 채우기와 미관측 면 색 채우기 (명세 5.3, S15P31A101-481).

texrecon 으로 굽은 `textured.glb` 는 면의 절반가량이 미관측 중립 회색(`unobserved_neutral`)이고, 테이블·벤치 밑처럼
가려진 바닥은 메시가 없어 검게 뚫린다. 이 모듈은 보기 좋게만 하는 두 가지를 더한다.

- (A) 바닥 평면: 관측된 바닥 정점(World z < 0.08 m)의 xy 1~99 분위 사각형, z = (z 5분위 − 0.02 m), 관측 바닥 색 중앙값 단색.
- (B) 미관측 면 색: `unobserved_neutral` 정점마다 반경 0.35 m 안 관측 정점 최근접 6개의 역거리 가중 평균색을 정점색(COLOR_0)으로.

지키는 것:

- 관측 메시·텍스처·좌표는 바꾸지 않는다. 입력 GLB 의 JSON·BIN 을 직접 읽고 새 데이터를 BIN 뒤에 덧붙인다(trimesh 불필요).
- 시각용이다(`geometry_role: VISUAL`, `safety_use: false`). 위험 판정·구역 좌표·`observed_surface_ratio`·출처 지도에 쓰지 않는다.
  바닥 평면은 출처 지도에 없는 추정 면이라 `extras.simai.enhanced.estimated_floor_plane.surface_source = ESTIMATED_PLANE` 로 표시한다.
- 채택 검사: 보정 카메라와 사람 지운 배경 사진이 있으면 각 카메라 시점에서 원본·보강본을 렌더해 사진과 MAE 를 비교하고,
  보강본이 허용폭보다 나빠지면 원본을 그대로 둔다. 검사 입력이 없으면 메시 지표(빈 바닥·미관측 회색 면적 감소)만으로 정하고 그 사실을 남긴다.
- World→glTF `(x, z, −y)` 변환은 `texture_mesh.to_glb_points` 한 곳에서만 한다(규칙 3). 읽을 때 되돌리는 쪽도 같은 상수(`GLTF_Y_UP`)를 쓴다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from .texture_mesh import GLTF_Y_UP, to_glb_points

ENHANCE_VERSION = "1.1.0"
NEUTRAL_NAMES = ("unobserved_neutral",)
UNSUPPORTED_REQUIRED = {"EXT_meshopt_compression", "KHR_draco_mesh_compression", "KHR_texture_basisu"}

_COMPONENT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_TYPE_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_NORMALIZE_DIV = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0}


class EnhanceSkip(Exception):
    """보강을 하지 않는 이유(입력이 지원 밖이거나 근거가 부족함). 원본을 그대로 둔다."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


class EnhanceTimeout(Exception):
    """보강에 쓸 수 있는 시간이 끝났다. 원본을 그대로 둔다."""


# --------------------------------------------------------------------------- 설정


@dataclass(frozen=True)
class EnhanceConfig:
    """설정값. 끄기·수치는 환경 변수(`from_env`) 또는 작업 옵션으로 바꾼다."""

    enabled: bool = True
    # (A) 바닥 평면
    floor_obs_z_max_m: float = 0.08  # 이 높이 아래 관측 정점을 바닥으로 본다
    floor_obs_z_min_m: float = -0.5  # 이 아래는 잡음으로 보고 버린다
    floor_xy_quantiles: tuple[float, float] = (1.0, 99.0)
    floor_z_quantile: float = 5.0
    floor_z_margin_m: float = 0.02  # 평면을 관측 바닥보다 이만큼 낮춰 겹침(z-fighting)을 피한다
    floor_min_points: int = 500
    floor_min_area_m2: float = 1.0
    coverage_cell_m: float = 0.05  # 위에서 본 빈 바닥 비율을 재는 격자
    # (B) 미관측 면 색
    neighbor_radius_m: float = 0.35
    neighbor_k: int = 6
    neighbor_eps_m: float = 1e-3
    # 채택 검사
    check_render_long_side: int = 640
    check_tolerance_mean: float = 0.002  # 평균 MAE(0~1) 가 이만큼보다 더 나빠지면 원본 유지
    check_tolerance_camera: float = 0.005  # 카메라 한 대의 MAE 가 이만큼보다 더 나빠져도 원본 유지
    check_min_pixels: int = 2000
    check_near_m: float = 0.05
    check_background_rgb: tuple[int, int, int] = (0, 0, 0)  # 메시가 없는 픽셀을 뷰어 바탕색으로 본다
    time_budget_s: float | None = None
    # (C) 늘어난 면 제거·바닥·벽 평면 채우기(S15P31A101-511, glb_fill.py). 보정 카메라·사진이 있을 때만 돈다
    fill_enabled: bool = True
    fill_stretched: bool = True
    fill_floor: bool = True
    fill_walls: bool = True
    fill_cos_slant: float = 0.15  # 보이는 모든 시선에서 |cos| 가 이보다 작은 면(약 81° 이상 사선)을 지운다(-505: 0.25 는 먼 바닥이 점점이 지워짐)
    fill_long_edge_m: float = 0.0  # > 0 이면 긴 변이 이보다 긴 면도 지운다(기본 끔: 단순화된 평평한 큰 면과 구분되지 않는다)
    fill_vis_long_side: int = 1152  # 면 가시성·깊이 버퍼 렌더 해상도(긴 변)
    fill_cell_m: float = 0.05  # 채움 면 격자
    fill_texel_m: float = 0.01  # 채움 텍스처 텍셀
    fill_floor_close_m: float = 0.6  # 바닥 칸 닫힘 반경
    fill_floor_reach_m: float = 2.5  # 카메라 광선 근거로 메시 바닥에서 더 넓힐 수 있는 거리
    fill_floor_ray_range_m: float = 30.0
    fill_floor_snap_m: float = 0.08  # |바닥 중앙값| 이하면 채움 평면을 World Z=0 에 둔다
    fill_color_mode: str = "consistent"  # strict | consistent | lenient (glb_fill.project_colors)
    fill_consistency_max_diff: int = 55  # consistent: 보이는 카메라 색 최대 채널 차이 허용(0~255)
    fill_wall_dist_tol_m: float = 0.06
    fill_wall_min_area_m2: float = 3.0
    fill_wall_min_len_m: float = 1.5
    fill_wall_max_outside: float = 0.08
    fill_wall_manhattan_deg: float = 8.0
    fill_wall_max_planes: int = 8
    fill_wall_ransac_iter: int = 1500
    fill_wall_max_height_m: float = 3.5
    fill_wall_max_fill_frac: float = 0.85
    # (D) 평면 인페인팅(S15P31A101-514, glb_inpaint.py). 사이드카 마스크(255)만 메워 GLB 에 되돌려 쓴다
    inpaint_mode: str = "classic"  # off | classic | diffusion (SIMAI_GLB_INPAINT)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, options: Mapping[str, Any] | None = None) -> EnhanceConfig:
        env = os.environ if env is None else env
        cfg = cls()
        kw: dict[str, Any] = {}
        if (raw := env.get("SIMAI_GLB_ENHANCE", "").strip()) != "":
            kw["enabled"] = raw.lower() not in ("0", "false", "off", "no")
        for name, field_name, cast in (
            ("SIMAI_ENHANCE_FLOOR_MARGIN_M", "floor_z_margin_m", float),
            ("SIMAI_ENHANCE_NEIGHBOR_RADIUS_M", "neighbor_radius_m", float),
            ("SIMAI_ENHANCE_NEIGHBOR_K", "neighbor_k", int),
            ("SIMAI_ENHANCE_CHECK_TOLERANCE_MEAN", "check_tolerance_mean", float),
            ("SIMAI_ENHANCE_CHECK_TOLERANCE_CAMERA", "check_tolerance_camera", float),
            ("SIMAI_FILL_COS_SLANT", "fill_cos_slant", float),
            ("SIMAI_FILL_COLOR_MODE", "fill_color_mode", str),
            ("SIMAI_FILL_FLOOR_REACH_M", "fill_floor_reach_m", float),
        ):
            if (raw := env.get(name, "").strip()) != "":
                kw[field_name] = cast(raw)
        if (raw := env.get("SIMAI_GLB_FILL", "").strip()) != "":
            kw["fill_enabled"] = raw.lower() not in ("0", "false", "off", "no")
        if options and isinstance(options.get("enhance"), bool):
            kw["enabled"] = options["enhance"]
        if options and isinstance(options.get("fill"), bool):
            kw["fill_enabled"] = options["fill"]
        if (raw := env.get("SIMAI_GLB_INPAINT", "").strip()) != "":
            low = raw.lower()
            kw["inpaint_mode"] = "off" if low in ("0", "false", "off", "no", "none", "disabled") else low
        if options and "inpaint" in options and options["inpaint"] is not None:
            v = options["inpaint"]
            if isinstance(v, bool):
                kw["inpaint_mode"] = "classic" if v else "off"
            else:
                low = str(v).strip().lower()
                kw["inpaint_mode"] = "off" if low in ("0", "false", "off", "no", "none", "disabled") else low
        return replace(cfg, **kw) if kw else cfg


# --------------------------------------------------------------------------- 색 공간


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = np.asarray(c, np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(np.asarray(c, np.float64), 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


_SRGB_LUT = srgb_to_linear(np.arange(256) / 255.0).astype(np.float32)


# --------------------------------------------------------------------------- glTF 읽기


@dataclass
class GlbFile:
    doc: dict[str, Any]
    bin: bytearray


def parse_glb(raw: bytes) -> GlbFile:
    if len(raw) < 20:
        raise EnhanceSkip("NOT_GLB", "glb 가 너무 짧다")
    magic, version, _ = struct.unpack_from("<4sII", raw, 0)
    if magic != b"glTF" or version != 2:
        raise EnhanceSkip("NOT_GLB", "glb 2.0 이 아니다")
    off = 12
    doc: dict[str, Any] | None = None
    blob = bytearray()
    while off + 8 <= len(raw):
        length, ctype = struct.unpack_from("<I4s", raw, off)
        body = raw[off + 8 : off + 8 + length]
        if ctype == b"JSON" and doc is None:
            doc = json.loads(body.decode("utf-8"))
        elif ctype == b"BIN\x00" and not blob:
            blob = bytearray(body)
        off += 8 + length
    if doc is None:
        raise EnhanceSkip("NOT_GLB", "JSON 덩어리 없음")
    return GlbFile(doc, blob)


def _accessor(g: GlbFile, index: int) -> np.ndarray:
    acc = g.doc["accessors"][index]
    if "sparse" in acc:
        raise EnhanceSkip("SPARSE_ACCESSOR", "희소 접근자는 지원하지 않는다")
    dtype = np.dtype(_COMPONENT[acc["componentType"]]).newbyteorder("<")
    n_comp = _TYPE_COUNT[acc["type"]]
    count = int(acc["count"])
    if "bufferView" not in acc:
        return np.zeros((count, n_comp), dtype)
    view = g.doc["bufferViews"][acc["bufferView"]]
    start = int(view.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
    stride = int(view.get("byteStride", 0)) or dtype.itemsize * n_comp
    if stride == dtype.itemsize * n_comp:
        out = np.frombuffer(g.bin, dtype, count * n_comp, start).reshape(count, n_comp)
    else:
        rows = np.frombuffer(g.bin, np.uint8, (count - 1) * stride + dtype.itemsize * n_comp, start) if count else np.zeros(0, np.uint8)
        out = np.lib.stride_tricks.as_strided(rows, (count, n_comp * dtype.itemsize), (stride, 1)).copy()
        out = out.view(dtype).reshape(count, n_comp)
    if acc.get("normalized") and acc["componentType"] in _NORMALIZE_DIV:
        return np.maximum(out.astype(np.float32) / _NORMALIZE_DIV[acc["componentType"]], -1.0)
    return out


def _node_matrix(node: Mapping[str, Any]) -> np.ndarray:
    if "matrix" in node:
        return np.asarray(node["matrix"], np.float64).reshape(4, 4).T
    t = np.asarray(node.get("translation", [0, 0, 0]), np.float64)
    q = np.asarray(node.get("rotation", [0, 0, 0, 1]), np.float64)
    s = np.asarray(node.get("scale", [1, 1, 1]), np.float64)
    x, y, z, w = q / np.linalg.norm(q)
    R = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    M = np.eye(4)
    M[:3, :3] = R * s[None, :]
    M[:3, 3] = t
    return M


def _decode_image(g: GlbFile, index: int) -> np.ndarray:
    import cv2

    image = g.doc["images"][index]
    if "bufferView" not in image:
        raise EnhanceSkip("EXTERNAL_IMAGE", "외부 이미지 주소는 지원하지 않는다")
    view = g.doc["bufferViews"][image["bufferView"]]
    start = int(view.get("byteOffset", 0))
    data = np.frombuffer(g.bin, np.uint8, int(view["byteLength"]), start)
    bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if bgr is None:
        raise EnhanceSkip("IMAGE_DECODE_FAILED", f"이미지 {index} 를 읽지 못했다")
    return np.ascontiguousarray(bgr[:, :, ::-1])


# --------------------------------------------------------------------------- 장면 모델


@dataclass
class Prim:
    """한 삼각형 프리미티브. 좌표는 World(미터, +Z 상방), 색은 선형 RGB."""

    name: str
    kind: str  # observed | unobserved | estimated
    pos: np.ndarray  # (n, 3) World
    idx: np.ndarray  # (m, 3) int
    uv: np.ndarray | None = None  # (n, 2)
    col: np.ndarray | None = None  # (n, 3) 선형 정점색(COLOR_0 또는 새로 채운 색)
    factor: np.ndarray = field(default_factory=lambda: np.ones(3))  # 선형 baseColorFactor
    image: int | None = None  # 이미지 번호(텍스처가 있을 때)
    wrap: tuple[int, int] = (10497, 10497)
    # 문서 안 위치(출력 때만 쓴다)
    mesh_index: int = -1
    prim_index: int = -1


@dataclass
class SceneModel:
    prims: list[Prim]
    images: dict[int, np.ndarray]  # 이미지 번호 → (H, W, 3) uint8 RGB. 필요할 때만 채운다


def check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise EnhanceTimeout("시간 예산 소진")


def _glb_to_world(P_glb: np.ndarray) -> np.ndarray:
    # 내보낼 때 glb = world @ G.T 였으므로 world = glb @ G (G 는 회전이라 G.T 의 역이 G).
    return np.asarray(P_glb, np.float64) @ GLTF_Y_UP


def load_scene(g: GlbFile) -> SceneModel:
    doc = g.doc
    if UNSUPPORTED_REQUIRED & set(doc.get("extensionsRequired", [])):
        raise EnhanceSkip("COMPRESSED_GLB", "압축 확장(meshopt·draco·KTX2)이 필요한 glb 는 보강하지 않는다")
    simai = (doc.get("extras") or {}).get("simai") if isinstance(doc.get("extras"), dict) else None
    if isinstance(simai, dict) and simai.get("axis_convention") not in (None, "GLTF_Y_UP"):
        raise EnhanceSkip("AXIS_NOT_WORLD", f"axis_convention={simai.get('axis_convention')}")
    if isinstance(simai, dict) and isinstance(simai.get("enhanced"), dict):
        raise EnhanceSkip("ALREADY_ENHANCED", "이미 보강된 glb")
    if not doc.get("meshes") or not doc.get("nodes"):
        raise EnhanceSkip("EMPTY_SCENE", "메시 없음")
    # 노드 세계 행렬
    scene_nodes = doc["scenes"][doc.get("scene", 0)].get("nodes", []) if doc.get("scenes") else list(range(len(doc["nodes"])))
    mesh_nodes: list[tuple[int, np.ndarray, str]] = []
    stack = [(n, np.eye(4)) for n in scene_nodes]
    seen: set[int] = set()
    while stack:
        n, parent = stack.pop()
        if n in seen:
            raise EnhanceSkip("NODE_CYCLE", "노드 그래프가 트리가 아니다")
        seen.add(n)
        node = doc["nodes"][n]
        M = parent @ _node_matrix(node)
        if "mesh" in node:
            mesh_nodes.append((int(node["mesh"]), M, str(node.get("name", ""))))
        stack.extend((c, M) for c in node.get("children", []))
    used = [m for m, _, _ in mesh_nodes]
    if len(used) != len(set(used)):
        raise EnhanceSkip("MULTI_INSTANCE", "같은 메시를 여러 노드가 쓴다")
    prims: list[Prim] = []
    materials = doc.get("materials", [])
    textures = doc.get("textures", [])
    samplers = doc.get("samplers", [])
    for mesh_index, M, node_name in mesh_nodes:
        mesh = doc["meshes"][mesh_index]
        for prim_index, p in enumerate(mesh["primitives"]):
            if p.get("mode", 4) != 4:
                continue
            attrs = p["attributes"]
            if "POSITION" not in attrs:
                continue
            P = _accessor(g, attrs["POSITION"]).astype(np.float64)
            P = P @ M[:3, :3].T + M[:3, 3]
            if "indices" in p:
                idx = _accessor(g, p["indices"]).reshape(-1).astype(np.int64)
            else:
                idx = np.arange(len(P), dtype=np.int64)
            idx = idx[: len(idx) // 3 * 3].reshape(-1, 3)
            mat = materials[p["material"]] if "material" in p else {}
            mat_name = str(mat.get("name", ""))
            pbr = mat.get("pbrMetallicRoughness", {})
            factor_srgb_lin = np.asarray(pbr.get("baseColorFactor", [1, 1, 1, 1])[:3], np.float64)
            uv = None
            image = None
            wrap = (10497, 10497)
            tex_info = pbr.get("baseColorTexture")
            if tex_info is not None and "TEXCOORD_0" in attrs and tex_info.get("texCoord", 0) == 0:
                tex = textures[tex_info["index"]]
                if "source" in tex and tex["source"] is not None:
                    image = int(tex["source"])
                    uv = _accessor(g, attrs["TEXCOORD_0"]).astype(np.float64)
                    if "sampler" in tex:
                        sm = samplers[tex["sampler"]]
                        wrap = (int(sm.get("wrapS", 10497)), int(sm.get("wrapT", 10497)))
            col = None
            if "COLOR_0" in attrs:
                c = _accessor(g, attrs["COLOR_0"]).astype(np.float64)
                col = c[:, :3]
            is_neutral = any(s in NEUTRAL_NAMES for s in (mat_name, str(mesh.get("name", "")), node_name))
            kind = "unobserved" if (is_neutral and image is None) else "observed"
            prims.append(Prim(
                name=str(mesh.get("name", node_name)) or f"mesh{mesh_index}", kind=kind, pos=_glb_to_world(P), idx=idx,
                uv=uv, col=col, factor=factor_srgb_lin, image=image, wrap=wrap,
                mesh_index=mesh_index, prim_index=prim_index))
    if not any(p.kind == "observed" for p in prims):
        raise EnhanceSkip("NO_OBSERVED_MESH", "관측 메시 없음")
    return SceneModel(prims, {})


def _image(g: GlbFile, model: SceneModel, index: int) -> np.ndarray:
    img = model.images.get(index)
    if img is None:
        img = _decode_image(g, index)
        model.images[index] = img
    return img


def sample_texture(img: np.ndarray, uv: np.ndarray, wrap: tuple[int, int] = (10497, 10497)) -> np.ndarray:
    """glTF UV(원점 왼쪽 위)로 최근접 텍셀을 읽는다. (n, 3) uint8."""
    h, w = img.shape[:2]
    u, v = uv[:, 0], uv[:, 1]
    out = []
    for coord, size, mode in ((u, w, wrap[0]), (v, h, wrap[1])):
        if mode == 33071:  # CLAMP_TO_EDGE
            c = np.clip(coord, 0.0, 1.0)
        elif mode == 33648:  # MIRRORED_REPEAT
            c = 1.0 - np.abs((coord % 2.0) - 1.0)
        else:
            c = coord % 1.0
        out.append(np.clip((c * size).astype(np.int64), 0, size - 1))
    return img[out[1], out[0]]


def prim_vertex_colors(g: GlbFile, model: SceneModel, prim: Prim, deadline: float | None = None) -> np.ndarray:
    """프리미티브 정점마다 선형 RGB 색(텍스처 × 정점색 × baseColorFactor)."""
    check_deadline(deadline)
    n = len(prim.pos)
    lin = np.ones((n, 3))
    if prim.image is not None and prim.uv is not None:
        texel = sample_texture(_image(g, model, prim.image), prim.uv, prim.wrap)
        lin = _SRGB_LUT[texel].astype(np.float64)
    if prim.col is not None:
        lin = lin * prim.col
    return lin * prim.factor[None, :]


# --------------------------------------------------------------------------- 래스터라이저


def rasterize(tri_xy: np.ndarray, tri_z: np.ndarray, shape: tuple[int, int], *, perspective: bool,
              max_fragments: int = 1_000_000, deadline: float | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """삼각형 z-버퍼 래스터라이즈(numpy). 화소 중심은 (j+0.5, i+0.5).

    tri_xy (T, 3, 2) 화면 좌표, tri_z (T, 3) 깊이(카메라 z 또는 위쪽 거리: 작을수록 가까움).
    perspective=True 이면 1/z 로 원근 보정한 무게를 낸다. 반환: tid (H, W) int64(-1 = 없음), weights (H, W, 3), depth (H, W).
    """
    H, W = shape
    tid_buf = np.full(H * W, -1, np.int64)
    w_buf = np.zeros((H * W, 3), np.float32)
    z_buf = np.full(H * W, np.inf, np.float64)
    if len(tri_xy) == 0:
        return tid_buf.reshape(H, W), w_buf.reshape(H, W, 3), z_buf.reshape(H, W)
    x, y = tri_xy[:, :, 0], tri_xy[:, :, 1]
    x0, x1, x2 = x[:, 0], x[:, 1], x[:, 2]
    y0, y1, y2 = y[:, 0], y[:, 1], y[:, 2]
    denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    j_min = np.maximum(np.ceil(x.min(1) - 0.5), 0).astype(np.int64)
    j_max = np.minimum(np.floor(x.max(1) - 0.5), W - 1).astype(np.int64)
    i_min = np.maximum(np.ceil(y.min(1) - 0.5), 0).astype(np.int64)
    i_max = np.minimum(np.floor(y.max(1) - 0.5), H - 1).astype(np.int64)
    keep = (np.abs(denom) > 1e-12) & (j_max >= j_min) & (i_max >= i_min) & np.isfinite(denom)
    ids = np.nonzero(keep)[0]
    if len(ids) == 0:
        return tid_buf.reshape(H, W), w_buf.reshape(H, W, 3), z_buf.reshape(H, W)
    size = np.maximum(j_max[ids] - j_min[ids], i_max[ids] - i_min[ids]) + 1
    bucket = np.ceil(np.log2(size)).astype(np.int64)  # 2**bucket >= size
    inv_z = 1.0 / tri_z if perspective else None
    for b in np.unique(bucket):
        sel_all = ids[bucket == b]
        side = 1 << int(b)
        chunk = max(1, max_fragments // (side * side))
        offs = np.arange(side)
        for s in range(0, len(sel_all), chunk):
            check_deadline(deadline)
            sel = sel_all[s : s + chunk]
            px = j_min[sel][:, None, None] + offs[None, None, :]
            py = i_min[sel][:, None, None] + offs[None, :, None]
            px, py = np.broadcast_arrays(px, py)
            xc, yc = px + 0.5, py + 0.5
            d = denom[sel][:, None, None]
            ax2, ay2 = x2[sel][:, None, None], y2[sel][:, None, None]
            l0 = ((y1[sel] - y2[sel])[:, None, None] * (xc - ax2) + (x2[sel] - x1[sel])[:, None, None] * (yc - ay2)) / d
            l1 = ((y2[sel] - y0[sel])[:, None, None] * (xc - ax2) + (x0[sel] - x2[sel])[:, None, None] * (yc - ay2)) / d
            l2 = 1.0 - l0 - l1
            eps = -1e-4
            inside = (l0 >= eps) & (l1 >= eps) & (l2 >= eps) & (px <= j_max[sel][:, None, None]) & (py <= i_max[sel][:, None, None])
            if not inside.any():
                continue
            ti = np.broadcast_to(np.arange(len(sel))[:, None, None], inside.shape)[inside]
            lam = np.stack([l0[inside], l1[inside], l2[inside]], axis=1)
            tri = sel[ti]
            if perspective:
                pw = lam * inv_z[tri]
                total = pw.sum(1)
                depth = 1.0 / total
                lam = pw / total[:, None]
            else:
                depth = (lam * tri_z[tri]).sum(1)
            pix = (py[inside] * W + px[inside]).astype(np.int64)
            order = np.lexsort((depth, pix))
            pix_s = pix[order]
            first = np.ones(len(pix_s), bool)
            first[1:] = pix_s[1:] != pix_s[:-1]
            win = order[first]
            pw_pix = pix[win]
            better = depth[win] < z_buf[pw_pix]
            win, pw_pix = win[better], pw_pix[better]
            z_buf[pw_pix] = depth[win]
            tid_buf[pw_pix] = tri[win]
            w_buf[pw_pix] = lam[win].astype(np.float32)
    return tid_buf.reshape(H, W), w_buf.reshape(H, W, 3), z_buf.reshape(H, W)


# --------------------------------------------------------------------------- 위에서 본 바닥 덮임


def top_down_coverage(prims: Sequence[Prim], rect: tuple[float, float, float, float], cell_m: float,
                      deadline: float | None = None) -> float:
    """rect=(x0, y0, x1, y1) 안에서 어떤 면이든 위에서 보이는 격자 칸 비율(0~1)."""
    x0, y0, x1, y1 = rect
    nx = max(1, math.ceil((x1 - x0) / cell_m))
    ny = max(1, math.ceil((y1 - y0) / cell_m))
    tri_xy, tri_z = [], []
    for p in prims:
        if not len(p.idx):
            continue
        P = p.pos[p.idx]  # (m, 3, 3)
        tri_xy.append(np.stack([(P[:, :, 0] - x0) / cell_m, (P[:, :, 1] - y0) / cell_m], axis=2))
        tri_z.append(-P[:, :, 2])  # 위(+z)일수록 가깝다
    if not tri_xy:
        return 0.0
    tid, _, _ = rasterize(np.concatenate(tri_xy), np.concatenate(tri_z), (ny, nx), perspective=False, deadline=deadline)
    return float((tid >= 0).mean())


# --------------------------------------------------------------------------- (A) 바닥 평면, (B) 미관측 색


@dataclass
class FloorPlane:
    rect: tuple[float, float, float, float]
    z: float
    color_lin: np.ndarray
    area_m2: float
    sample_count: int


def build_floor_plane(model: SceneModel, vertex_colors: Mapping[int, np.ndarray], cfg: EnhanceConfig) -> FloorPlane:
    pts, cols = [], []
    for i, p in enumerate(model.prims):
        if p.kind != "observed":
            continue
        used = np.unique(p.idx)
        z = p.pos[used, 2]
        sel = used[(z < cfg.floor_obs_z_max_m) & (z > cfg.floor_obs_z_min_m)]
        if len(sel):
            pts.append(p.pos[sel])
            cols.append(vertex_colors[i][sel])
    if not pts:
        raise EnhanceSkip("NO_FLOOR_POINTS", "바닥 관측점 없음")
    P, C = np.concatenate(pts), np.concatenate(cols)
    if len(P) < cfg.floor_min_points:
        raise EnhanceSkip("FEW_FLOOR_POINTS", f"바닥 관측점 {len(P)} < {cfg.floor_min_points}")
    qlo, qhi = cfg.floor_xy_quantiles
    x0, x1 = np.percentile(P[:, 0], [qlo, qhi])
    y0, y1 = np.percentile(P[:, 1], [qlo, qhi])
    area = float((x1 - x0) * (y1 - y0))
    if area < cfg.floor_min_area_m2:
        raise EnhanceSkip("SMALL_FLOOR", f"바닥 사각형 {area:.2f} m² < {cfg.floor_min_area_m2}")
    z = float(np.percentile(P[:, 2], cfg.floor_z_quantile) - cfg.floor_z_margin_m)
    color = np.median(C, axis=0)
    return FloorPlane((float(x0), float(y0), float(x1), float(y1)), z, color, area, len(P))


def floor_prim(fp: FloorPlane) -> Prim:
    x0, y0, x1, y1 = fp.rect
    pos = np.array([[x0, y0, fp.z], [x1, y0, fp.z], [x1, y1, fp.z], [x0, y1, fp.z]], np.float64)
    return Prim(name="estimated_floor_plane", kind="estimated", pos=pos, idx=np.array([[0, 1, 2], [0, 2, 3]], np.int64),
                factor=np.asarray(fp.color_lin, np.float64))


def _tri_area(pos: np.ndarray, idx: np.ndarray) -> np.ndarray:
    P = pos[idx]
    return 0.5 * np.linalg.norm(np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]), axis=1)


@dataclass
class Recolor:
    prim_index: int  # model.prims 안 위치
    col: np.ndarray  # (n, 3) 선형
    filled: np.ndarray  # (n,) bool: 이웃 색을 받은 정점


def recolor_unobserved(model: SceneModel, vertex_colors: Mapping[int, np.ndarray], cfg: EnhanceConfig,
                       deadline: float | None = None) -> list[Recolor]:
    from scipy.spatial import cKDTree

    obs_pos, obs_col = [], []
    for i, p in enumerate(model.prims):
        if p.kind == "observed":
            obs_pos.append(p.pos)
            obs_col.append(vertex_colors[i])
    tree = cKDTree(np.concatenate(obs_pos))
    colors = np.concatenate(obs_col)
    out: list[Recolor] = []
    for i, p in enumerate(model.prims):
        if p.kind != "unobserved":
            continue
        check_deadline(deadline)
        base = np.tile(p.factor[None, :], (len(p.pos), 1))
        if p.col is not None:
            base = base * p.col
        dist, nn = tree.query(p.pos, k=cfg.neighbor_k, distance_upper_bound=cfg.neighbor_radius_m)
        dist = dist.reshape(len(p.pos), -1)
        nn = nn.reshape(len(p.pos), -1)
        ok = np.isfinite(dist)
        w = np.where(ok, 1.0 / (dist + cfg.neighbor_eps_m), 0.0)
        total = w.sum(1)
        filled = total > 0
        nn_safe = np.where(ok, nn, 0)
        avg = (colors[nn_safe] * w[:, :, None]).sum(1) / np.where(filled, total, 1.0)[:, None]
        out.append(Recolor(i, np.where(filled[:, None], avg, base), filled))
    return out


# --------------------------------------------------------------------------- 채택 검사: 카메라 렌더


@dataclass
class CheckCamera:
    camera_id: str
    K: np.ndarray  # 3x3. image 와 같은 (왜곡 편) 화소 좌표계
    R: np.ndarray  # World→camera (OPENCV)
    t: np.ndarray
    image: np.ndarray  # (H, W, 3) uint8 RGB. 사람 지운 배경 사진
    valid: np.ndarray | None = None  # (H, W) bool. 비교할 화소(사람 마스크 밖)


def _flatten(prims: Sequence[Prim]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    pos, uv, col, pid = [], [], [], []
    for k, p in enumerate(prims):
        if not len(p.idx):
            continue
        pos.append(p.pos[p.idx])
        uv.append(p.uv[p.idx] if p.uv is not None else np.zeros((len(p.idx), 3, 2)))
        col.append(p.col[p.idx] if p.col is not None else np.ones((len(p.idx), 3, 3)))
        pid.append(np.full(len(p.idx), k, np.int32))
    return np.concatenate(pos), np.concatenate(uv), np.concatenate(col), np.concatenate(pid)


def _split4(pos, uv, col, pid):
    a, b, c = pos[:, 0], pos[:, 1], pos[:, 2]
    ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2

    def cat(parts):
        return np.concatenate(parts, axis=0)

    def mk(x, y, z):
        return np.stack([x, y, z], axis=1)

    new_pos = cat([mk(a, ab, ca), mk(ab, b, bc), mk(ca, bc, c), mk(ab, bc, ca)])
    ua, ub, uc = uv[:, 0], uv[:, 1], uv[:, 2]
    uab, ubc, uca = (ua + ub) / 2, (ub + uc) / 2, (uc + ua) / 2
    new_uv = cat([mk(ua, uab, uca), mk(uab, ub, ubc), mk(uca, ubc, uc), mk(uab, ubc, uca)])
    ca_, cb_, cc_ = col[:, 0], col[:, 1], col[:, 2]
    cab, cbc, cca = (ca_ + cb_) / 2, (cb_ + cc_) / 2, (cc_ + ca_) / 2
    new_col = cat([mk(ca_, cab, cca), mk(cab, cb_, cbc), mk(cca, cbc, cc_), mk(cab, cbc, cca)])
    return new_pos, new_uv, new_col, np.tile(pid, 4)


def render_view(g: GlbFile, model: SceneModel, prims: Sequence[Prim], cam: CheckCamera, cfg: EnhanceConfig,
                deadline: float | None = None) -> tuple[np.ndarray, np.ndarray, float]:
    """카메라 시점에서 prims 를 그려 (sRGB uint8 영상, 덮임 마스크, 배율)을 낸다. 가까운 면 평면을 넘는 큰 면은 쪼개 그린다."""
    h0, w0 = cam.image.shape[:2]
    scale = min(1.0, cfg.check_render_long_side / max(h0, w0))
    H, W = max(1, round(h0 * scale)), max(1, round(w0 * scale))
    pos, uv, col, pid = _flatten(prims)
    R, t = np.asarray(cam.R, np.float64), np.asarray(cam.t, np.float64).reshape(3)
    for _ in range(7):
        z = pos @ R[2] + t[2]
        crossing = (z.min(1) < cfg.check_near_m) & (z.max(1) >= cfg.check_near_m)
        if not crossing.any():
            break
        keep = ~crossing
        n_pos, n_uv, n_col, n_pid = _split4(pos[crossing], uv[crossing], col[crossing], pid[crossing])
        pos, uv, col, pid = (np.concatenate([a[keep], b]) for a, b in ((pos, n_pos), (uv, n_uv), (col, n_col), (pid, n_pid)))
    cam_pts = pos @ R.T + t
    front = cam_pts[:, :, 2].min(1) >= cfg.check_near_m
    pos, uv, col, pid, cam_pts = pos[front], uv[front], col[front], pid[front], cam_pts[front]
    K = np.asarray(cam.K, np.float64)
    zc = cam_pts[:, :, 2]
    u = (K[0, 0] * cam_pts[:, :, 0] / zc + K[0, 2] + 0.5) * (W / w0)
    v = (K[1, 1] * cam_pts[:, :, 1] / zc + K[1, 2] + 0.5) * (H / h0)
    tid, lam, _ = rasterize(np.stack([u, v], axis=2), zc, (H, W), perspective=True, deadline=deadline)
    covered = tid >= 0
    out = np.empty((H, W, 3), np.float64)
    bg = srgb_to_linear(np.asarray(cfg.check_background_rgb, np.float64) / 255.0)
    out[:] = bg
    ys, xs = np.nonzero(covered)
    if len(ys):
        t_ids = tid[ys, xs]
        wts = lam[ys, xs].astype(np.float64)
        pix_pid = pid[t_ids]
        lin = np.ones((len(ys), 3))
        for k in np.unique(pix_pid):
            check_deadline(deadline)
            m = pix_pid == k
            prim = prims[int(k)]
            tri = t_ids[m]
            w3 = wts[m]
            c = np.ones((int(m.sum()), 3))
            if prim.image is not None and prim.uv is not None:
                puv = (uv[tri] * w3[:, :, None]).sum(1)
                c = _SRGB_LUT[sample_texture(_image(g, model, prim.image), puv, prim.wrap)].astype(np.float64)
            if prim.col is not None:
                c = c * (col[tri] * w3[:, :, None]).sum(1)
            lin[m] = c * prim.factor[None, :]
        out[ys, xs] = lin
    img = np.rint(linear_to_srgb(out) * 255.0).astype(np.uint8)
    return img, covered, scale


def _mae(render: np.ndarray, photo: np.ndarray, valid: np.ndarray) -> float:
    d = np.abs(render.astype(np.float32) - photo.astype(np.float32)) / 255.0
    return float(d.mean(axis=2)[valid].mean())


def run_check(g: GlbFile, model: SceneModel, original: Sequence[Prim], enhanced: Sequence[Prim],
              cameras: Sequence[CheckCamera], cfg: EnhanceConfig, deadline: float | None = None) -> dict[str, Any]:
    """카메라별 MAE(0~1) 전후. 채택 조건은 평균이 tolerance_mean, 카메라별이 tolerance_camera 안."""
    import cv2

    per: dict[str, Any] = {}
    used_before, used_after = [], []
    for cam in cameras:
        check_deadline(deadline)
        before, _, scale = render_view(g, model, original, cam, cfg, deadline)
        after, _, _ = render_view(g, model, enhanced, cam, cfg, deadline)
        H, W = before.shape[:2]
        photo = cv2.resize(cam.image, (W, H), interpolation=cv2.INTER_AREA)
        valid = np.ones((H, W), bool) if cam.valid is None else cv2.resize(cam.valid.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST).astype(bool)
        n = int(valid.sum())
        if n < cfg.check_min_pixels:
            per[cam.camera_id] = {"pixels": n, "used": False, "reason": "FEW_VALID_PIXELS"}
            continue
        mb, ma = _mae(before, photo, valid), _mae(after, photo, valid)
        per[cam.camera_id] = {"pixels": n, "used": True, "render_scale": round(scale, 4), "mae_before": round(mb, 5),
                              "mae_after": round(ma, 5), "delta": round(ma - mb, 5)}
        used_before.append(mb)
        used_after.append(ma)
    if not used_before:
        return {"mode": "RENDER_MAE", "usable": False, "reason": "NO_USABLE_CAMERA", "cameras": per}
    mean_b, mean_a = float(np.mean(used_before)), float(np.mean(used_after))
    worst = max(a - b for a, b in zip(used_after, used_before))
    accepted = (mean_a <= mean_b + cfg.check_tolerance_mean) and (worst <= cfg.check_tolerance_camera)
    return {"mode": "RENDER_MAE", "usable": True, "accepted": bool(accepted), "mae_mean_before": round(mean_b, 5),
            "mae_mean_after": round(mean_a, 5), "mae_worst_camera_delta": round(float(worst), 5),
            "tolerance_mean": cfg.check_tolerance_mean, "tolerance_camera": cfg.check_tolerance_camera, "cameras": per}


# --------------------------------------------------------------------------- 출력 glb


def _add_view(g: GlbFile, data: bytes, target: int | None) -> int:
    g.bin += b"\x00" * (-len(g.bin) % 4)
    view: dict[str, Any] = {"buffer": 0, "byteOffset": len(g.bin), "byteLength": len(data)}
    if target is not None:
        view["target"] = target
    g.bin += data
    g.doc.setdefault("bufferViews", []).append(view)
    return len(g.doc["bufferViews"]) - 1


def _add_accessor(g: GlbFile, view: int, component: int, count: int, type_: str, **extra: Any) -> int:
    g.doc.setdefault("accessors", []).append({"bufferView": view, "byteOffset": 0, "componentType": component, "count": count, "type": type_, **extra})
    return len(g.doc["accessors"]) - 1


def serialize_glb(g: GlbFile) -> bytes:
    g.bin += b"\x00" * (-len(g.bin) % 4)
    if g.doc.get("buffers"):
        g.doc["buffers"][0]["byteLength"] = len(g.bin)
        g.doc["buffers"][0].pop("uri", None)
    else:
        g.doc["buffers"] = [{"byteLength": len(g.bin)}]
    js = json.dumps(g.doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(g.bin)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(js), b"JSON") + js
            + struct.pack("<I4s", len(g.bin), b"BIN\x00") + bytes(g.bin))


def _write_fill(g: GlbFile, model: SceneModel, fill: Any) -> None:
    """glb_fill 결과를 문서에 덧붙인다: 늘어난 면을 뺀 새 색인, 채움 면(텍스처 + 출처 지도 PNG)."""
    from .glb_fill import SRC_GRAY, SRC_PROJECTED, encode_png

    doc = g.doc
    model_prims = model.prims
    for k, idx in fill.new_idx.items():
        prim = model_prims[k]
        src = doc["meshes"][prim.mesh_index]["primitives"][prim.prim_index]
        data = np.ascontiguousarray(idx.reshape(-1), dtype="<u4").tobytes()
        src["indices"] = _add_accessor(g, _add_view(g, data, 34963), 5125, int(idx.size), "SCALAR")
    if not fill.fills:
        return
    materials = doc.setdefault("materials", [])
    samplers = doc.setdefault("samplers", [])
    samplers.append({"magFilter": 9729, "minFilter": 9729, "wrapS": 33071, "wrapT": 33071})
    sampler = len(samplers) - 1
    for fp in fill.fills:
        images = doc.setdefault("images", [])
        images.append({"bufferView": _add_view(g, encode_png(fp.image), None), "mimeType": "image/png", "name": f"estimated_{fp.role}_albedo"})
        albedo = len(images) - 1
        images.append({"bufferView": _add_view(g, encode_png(fp.source), None), "mimeType": "image/png", "name": f"estimated_{fp.role}_surface_source"})
        source = len(images) - 1
        textures = doc.setdefault("textures", [])
        textures.append({"source": albedo, "sampler": sampler})
        tex = len(textures) - 1
        materials.append({"name": f"estimated_{fp.role}_fill", "doubleSided": True,
                          "pbrMetallicRoughness": {"baseColorTexture": {"index": tex}, "metallicFactor": 0.0, "roughnessFactor": 1.0}})
        V = to_glb_points(np.eye(4), fp.prim.pos)  # World → glTF 변환은 이 어댑터 한 곳
        nrm = np.tile((np.asarray(fp.normal_world, float) @ GLTF_Y_UP.T)[None, :], (len(V), 1))
        pos_acc = _add_accessor(g, _add_view(g, np.ascontiguousarray(V, dtype="<f4").tobytes(), 34962), 5126, len(V), "VEC3",
                                min=[float(x) for x in V.min(0)], max=[float(x) for x in V.max(0)])
        nrm_acc = _add_accessor(g, _add_view(g, np.ascontiguousarray(nrm, dtype="<f4").tobytes(), 34962), 5126, len(V), "VEC3")
        uv_acc = _add_accessor(g, _add_view(g, np.ascontiguousarray(fp.prim.uv, dtype="<f4").tobytes(), 34962), 5126, len(V), "VEC2")
        idx_acc = _add_accessor(g, _add_view(g, np.ascontiguousarray(fp.prim.idx.reshape(-1), dtype="<u4").tobytes(), 34963), 5125,
                                int(fp.prim.idx.size), "SCALAR")
        doc.setdefault("meshes", []).append({"name": f"estimated_{fp.role}_fill", "primitives": [
            {"attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc, "TEXCOORD_0": uv_acc}, "indices": idx_acc, "material": len(materials) - 1, "mode": 4}]})
        ratios = {"ESTIMATED_PLANE": fp.stats["projected_ratio"], "UNOBSERVED": fp.stats["gray_ratio"]}
        doc["nodes"].append({"name": f"estimated_{fp.role}_fill", "mesh": len(doc["meshes"]) - 1, "extras": {"simai": {
            "surface_source": "ESTIMATED_PLANE", "geometry_role": "VISUAL", "safety_use": False, "source_map_image": source,
            "source_values": {"ESTIMATED_PLANE": SRC_PROJECTED, "UNOBSERVED": SRC_GRAY}, "texel_ratios": ratios,
            "fill_area_m2": fp.stats["fill_m2"], "color_mode": fp.stats["color_mode"]}}})
        doc["scenes"][doc.get("scene", 0)].setdefault("nodes", []).append(len(doc["nodes"]) - 1)


def write_enhanced(g: GlbFile, model: SceneModel, recolors: Sequence[Recolor], floor: FloorPlane | None, enhanced_meta: dict[str, Any],
                   fill: Any = None) -> bytes:
    """원본 문서에 덧붙여 보강 glb 를 만든다. 관측 메시·텍스처·좌표는 그대로 둔다(늘어난 면 제거는 색인만 새로 쓴다)."""
    doc = g.doc
    if fill is not None:
        _write_fill(g, model, fill)
    materials = doc.setdefault("materials", [])
    for rc in recolors:
        prim = model.prims[rc.prim_index]
        src = doc["meshes"][prim.mesh_index]["primitives"][prim.prim_index]
        data = np.ascontiguousarray(rc.col, dtype="<f4").tobytes()
        view = _add_view(g, data, 34962)
        acc = _add_accessor(g, view, 5126, len(rc.col), "VEC3")
        src["attributes"]["COLOR_0"] = acc
        old = materials[src["material"]] if "material" in src else {"name": "unobserved_neutral"}
        new = json.loads(json.dumps(old))
        new["name"] = f"{old.get('name', 'unobserved_neutral')}_recolored"
        pbr = new.setdefault("pbrMetallicRoughness", {})
        pbr["baseColorFactor"] = [1.0, 1.0, 1.0, float((old.get("pbrMetallicRoughness", {}).get("baseColorFactor") or [1, 1, 1, 1])[3])]
        materials.append(new)
        src["material"] = len(materials) - 1
    if floor is not None:
        fp = floor_prim(floor)
        V = to_glb_points(np.eye(4), fp.pos)  # World → glTF 변환은 이 어댑터 한 곳
        pos_view = _add_view(g, np.ascontiguousarray(V, dtype="<f4").tobytes(), 34962)
        pos_acc = _add_accessor(g, pos_view, 5126, 4, "VEC3", min=[float(x) for x in V.min(0)], max=[float(x) for x in V.max(0)])
        normals = np.tile(np.array([[0.0, 1.0, 0.0]], np.float32), (4, 1))
        nrm_acc = _add_accessor(g, _add_view(g, normals.astype("<f4").tobytes(), 34962), 5126, 4, "VEC3")
        idx_acc = _add_accessor(g, _add_view(g, np.ascontiguousarray(fp.idx, dtype="<u2").tobytes(), 34963), 5123, 6, "SCALAR")
        materials.append({"name": "estimated_floor_plane", "doubleSided": True,
                          "pbrMetallicRoughness": {"baseColorFactor": [*(float(x) for x in floor.color_lin), 1.0],
                                                   "metallicFactor": 0.0, "roughnessFactor": 1.0}})
        doc.setdefault("meshes", []).append({"name": "estimated_floor_plane", "primitives": [
            {"attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc}, "indices": idx_acc, "material": len(materials) - 1, "mode": 4}]})
        doc["nodes"].append({"name": "estimated_floor_plane", "mesh": len(doc["meshes"]) - 1,
                             "extras": {"simai": {"surface_source": "ESTIMATED_PLANE", "geometry_role": "VISUAL", "safety_use": False}}})
        scene = doc["scenes"][doc.get("scene", 0)]
        scene.setdefault("nodes", []).append(len(doc["nodes"]) - 1)
    extras = doc.setdefault("extras", {})
    if not isinstance(extras, dict):
        extras = doc["extras"] = {}
    simai = extras.setdefault("simai", {})
    if not isinstance(simai, dict):
        simai = extras["simai"] = {}
    simai["enhanced"] = enhanced_meta
    return serialize_glb(g)


# --------------------------------------------------------------------------- 공개 진입점


@dataclass
class EnhanceResult:
    glb: bytes | None  # 보강본 바이트. 적용하지 않으면 None(원본 유지)
    report: dict[str, Any]
    fills: list[Any] = field(default_factory=list)  # 적용된 채움 평면(glb_fill.FillPrim), 사이드카 저장용


def _round(x: Any, n: int = 5) -> Any:
    return round(float(x), n)


def enhance_glb(raw: bytes, *, config: EnhanceConfig | None = None, cameras: Sequence[CheckCamera] | None = None,
                clock=time.monotonic) -> EnhanceResult:
    """GLB 바이트를 보강한다. 실패·불채택이면 glb=None 이고 이유가 report 에 남는다. 예외로 호출자의 단계를 깨지 않는다."""
    cfg = config or EnhanceConfig()
    started = clock()
    report: dict[str, Any] = {"version": ENHANCE_VERSION, "applied": False, "reason": None, "purpose": "VISUAL_ONLY",
                              "safety_use": False, "config": _config_dict(cfg), "input_bytes": len(raw)}
    if not cfg.enabled:
        report["reason"] = "DISABLED"
        return EnhanceResult(None, report)
    deadline = (time.monotonic() + cfg.time_budget_s) if cfg.time_budget_s is not None else None
    try:
        g = parse_glb(raw)
        model = load_scene(g)
        report["prims"] = {"observed": sum(p.kind == "observed" for p in model.prims),
                           "unobserved": sum(p.kind == "unobserved" for p in model.prims)}
        vcols = {i: prim_vertex_colors(g, model, p, deadline) for i, p in enumerate(model.prims) if p.kind == "observed"}
        original = list(model.prims)
        steps: list[str] = []
        # (C) 늘어난 면 제거·바닥·벽 평면 채우기(보정 카메라·사진이 있을 때만). 실패해도 나머지 보강은 계속한다
        fill = None
        if cfg.fill_enabled and cameras:
            from .glb_fill import run_fill
            try:
                fill = run_fill(model, list(cameras), cfg, deadline)
                report["fill"] = {"applied": True, **fill.report}
                steps += fill.steps
            except EnhanceSkip as e:
                report["fill"] = {"applied": False, "reason": e.reason, "detail": str(e)}
        else:
            report["fill"] = {"applied": False, "reason": "DISABLED" if not cfg.fill_enabled else "NO_CAMERAS"}
        # (A)
        floor: FloorPlane | None = None
        try:
            if fill is not None and any(f.role == "floor" for f in fill.fills):
                raise EnhanceSkip("REPLACED_BY_FILL", "바닥은 구멍 채움(fill.floor)이 대신한다")
            floor = build_floor_plane(model, vcols, cfg)
            rect = floor.rect
            cov_before = top_down_coverage(original, rect, cfg.coverage_cell_m, deadline)
            cov_after = top_down_coverage([*original, floor_prim(floor)], rect, cfg.coverage_cell_m, deadline)
            report["floor"] = {"applied": True, "rect_xy_m": [_round(v, 4) for v in rect], "z_m": _round(floor.z, 4),
                               "area_m2": _round(floor.area_m2, 3), "sample_count": floor.sample_count,
                               "color_linear": [_round(v, 4) for v in floor.color_lin],
                               "color_srgb8": [round(float(v) * 255) for v in linear_to_srgb(floor.color_lin)],
                               "empty_ratio_before": _round(1 - cov_before, 4), "empty_ratio_after": _round(1 - cov_after, 4),
                               "filled_area_m2": _round((cov_after - cov_before) * floor.area_m2, 3),
                               "surface_source": "ESTIMATED_PLANE"}
            steps.append("floor_plane")
        except EnhanceSkip as e:
            report["floor"] = {"applied": False, "reason": e.reason}
        # (B)
        recolors: list[Recolor] = []
        if any(p.kind == "unobserved" for p in model.prims):
            recolors = recolor_unobserved(model, vcols, cfg, deadline)
            tot_faces = sum(len(model.prims[r.prim_index].idx) for r in recolors)
            area_before = sum(float(_tri_area(model.prims[r.prim_index].pos, model.prims[r.prim_index].idx).sum()) for r in recolors)
            area_gray_after = 0.0
            faces_gray_after = 0
            verts = filled_verts = 0
            for r in recolors:
                p = model.prims[r.prim_index]
                still = ~r.filled[p.idx].any(axis=1)  # 세 정점이 모두 못 채운 면만 회색으로 남는다
                faces_gray_after += int(still.sum())
                area_gray_after += float(_tri_area(p.pos, p.idx)[still].sum())
                verts += len(r.filled)
                filled_verts += int(r.filled.sum())
            report["recolor"] = {"applied": bool(filled_verts), "radius_m": cfg.neighbor_radius_m, "k": cfg.neighbor_k,
                                 "unobserved_faces": tot_faces, "unobserved_gray_faces_after": faces_gray_after,
                                 "unobserved_area_m2_before": _round(area_before, 3),
                                 "unobserved_gray_area_m2_after": _round(area_gray_after, 3),
                                 "recolored_area_m2": _round(area_before - area_gray_after, 3),
                                 "vertices": verts, "vertices_filled": filled_verts,
                                 "surface_source": "UNOBSERVED", "color_source": "INFERRED_FROM_NEIGHBOR_OBSERVED_VERTICES"}
            if filled_verts:
                steps.append("neutral_recolor")
            else:
                recolors = []
        else:
            report["recolor"] = {"applied": False, "reason": "NO_UNOBSERVED_MESH"}
        report["triangles_delta"] = (2 if floor is not None else 0) + (fill.triangles_delta if fill is not None else 0)
        if not steps:
            report["reason"] = "NOTHING_TO_DO"
            return EnhanceResult(None, report)
        # 보강본 모델
        enhanced = list(original)
        if fill is not None:
            for k, idx in fill.new_idx.items():
                enhanced[k] = replace(original[k], idx=idx)
            for f in fill.fills:
                enhanced.append(f.prim)
                model.images[f.prim.image] = f.image
        for r in recolors:
            enhanced[r.prim_index] = replace(enhanced[r.prim_index], col=r.col, factor=np.ones(3))
        if floor is not None:
            enhanced.append(floor_prim(floor))
        # 채택 검사
        if cameras:
            check = run_check(g, model, original, enhanced, cameras, cfg, deadline)
            if not check.get("usable"):
                check = {**check, "fallback": "MESH_METRICS_ONLY"}
                accepted = _mesh_metrics_accept(report)
                check["mode"] = "MESH_METRICS_ONLY"
                check["accepted"] = accepted
            else:
                accepted = bool(check["accepted"])
        else:
            accepted = _mesh_metrics_accept(report)
            check = {"mode": "MESH_METRICS_ONLY", "reason": "NO_CHECK_INPUT", "accepted": accepted,
                     "note": "보정 카메라·배경 사진이 없어 렌더 비교를 하지 못하고 메시 지표만으로 정했다"}
        report["check"] = check
        if not accepted:
            report["reason"] = "CHECK_REGRESSION" if check.get("mode") == "RENDER_MAE" else "MESH_METRICS_NOT_IMPROVED"
            return EnhanceResult(None, report)
        meta = {"version": ENHANCE_VERSION, "applied": True, "steps": steps, "purpose": "VISUAL_ONLY", "safety_use": False,
                "check_mode": check["mode"]}
        if "floor" in report and report["floor"].get("applied"):
            meta["estimated_floor_plane"] = {k: report["floor"][k] for k in ("rect_xy_m", "z_m", "area_m2", "color_srgb8", "surface_source", "filled_area_m2")}
        if report["recolor"].get("applied"):
            meta["neutral_recolor"] = {k: report["recolor"][k] for k in ("radius_m", "k", "recolored_area_m2", "surface_source", "color_source")}
        if fill is not None:
            meta["fill"] = {"steps": fill.steps, "removed_faces": fill.report["removed_faces"], "added_triangles": fill.report["added_triangles"],
                            "color_mode": cfg.fill_color_mode, "surface_source": fill.report["surface_source"]}
        if check["mode"] == "RENDER_MAE":
            meta["check"] = {k: check[k] for k in ("mae_mean_before", "mae_mean_after")}
        out = write_enhanced(g, model, recolors, floor, meta, fill)
        report.update(applied=True, reason=None, output_bytes=len(out))
        return EnhanceResult(out, report, list(fill.fills) if fill is not None else [])
    except EnhanceSkip as e:
        report["reason"] = e.reason
        report["detail"] = str(e)
    except EnhanceTimeout:
        report["reason"] = "TIME_BUDGET_EXCEEDED"
    except Exception as e:  # noqa: BLE001 - 시각 보강 실패가 복원 단계를 깨면 안 된다
        report["reason"] = f"ENHANCE_ERROR:{type(e).__name__}"
        report["detail"] = str(e)[:300]
    finally:
        report["seconds"] = round(clock() - started, 3)
    return EnhanceResult(None, report)


def _mesh_metrics_accept(report: Mapping[str, Any]) -> bool:
    """검사 입력이 없을 때: 빈 바닥 비율이나 미관측 회색 면적이 줄고 어느 쪽도 늘지 않으면 채택."""
    improved = False
    fi = report.get("fill") or {}
    if fi.get("applied") and fi.get("added_triangles", 0) > 0:
        improved = True  # 채움 면이 구멍을 메운다(채움 자체가 빈 곳에만 만든다)
    fl = report.get("floor", {})
    if fl.get("applied"):
        if fl["empty_ratio_after"] > fl["empty_ratio_before"]:
            return False
        improved |= fl["empty_ratio_after"] < fl["empty_ratio_before"]
    rc = report.get("recolor", {})
    if rc.get("applied"):
        if rc["unobserved_gray_area_m2_after"] > rc["unobserved_area_m2_before"]:
            return False
        improved |= rc["unobserved_gray_area_m2_after"] < rc["unobserved_area_m2_before"]
    return improved


def _config_dict(cfg: EnhanceConfig) -> dict[str, Any]:
    d = asdict(cfg)
    d["floor_xy_quantiles"] = list(cfg.floor_xy_quantiles)
    d["check_background_rgb"] = list(cfg.check_background_rgb)
    return d


def enhance_file(path: Path, *, config: EnhanceConfig | None = None, cameras: Sequence[CheckCamera] | None = None,
                 atlas_dir: Path | None = None, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """파일을 제자리에서 보강한다. 적용될 때만 임시 파일을 원자적으로 바꿔 쓴다. report 를 돌려준다.
    채움 평면이 있으면 평면별 이미지·인페인팅 마스크를 atlas_dir(기본: GLB 옆 `<이름>_fill/`)에 저장한다(실패해도 GLB 는 유지)."""
    res = enhance_glb(Path(path).read_bytes(), config=config, cameras=cameras)
    if res.glb is not None:
        tmp = Path(str(path) + ".enhance.tmp")
        tmp.write_bytes(res.glb)
        os.replace(tmp, path)
        if res.fills:
            from .glb_fill import write_sidecar
            d = Path(atlas_dir) if atlas_dir is not None else Path(path).with_name(Path(path).stem + "_fill")
            try:
                write_sidecar(res.fills, d)
                res.report["fill_atlas"] = {"dir": d.name, "index": "fill_atlas.json", "planes": [f.role for f in res.fills]}
                from .glb_inpaint import InpaintConfig, inpaint_sidecar_to_glb
                inp_opts = dict(options or {})
                if config is not None and "inpaint" not in inp_opts:
                    if config.inpaint_mode != "classic" or not os.environ.get("SIMAI_GLB_INPAINT", "").strip():
                        inp_opts["inpaint"] = config.inpaint_mode
                inp_cfg = InpaintConfig.from_env(options=inp_opts)
                res.report["inpaint"] = inpaint_sidecar_to_glb(path, d, config=inp_cfg, fill_report=res.report.get("fill"))
            except OSError as e:
                res.report["fill_atlas"] = {"error": type(e).__name__}
    return res.report


# --------------------------------------------------------------------------- 검사 입력 읽기


def cameras_from_prepared(root: Path) -> list[CheckCamera]:
    """static_reconstruct 작업 폴더: calibration.json + prepared/prepared.json(왜곡 편 영상·마스크)."""
    import cv2

    cal = json.loads((root / "calibration.json").read_text(encoding="utf-8"))
    prepared = json.loads((root / "prepared" / "prepared.json").read_text(encoding="utf-8"))
    by_id = {c["camera_id"]: c for c in cal["cameras"]}
    cams: list[CheckCamera] = []
    for v in prepared["views"]:
        c = by_id.get(v["camera_id"])
        bgr = cv2.imread(str(root / "prepared" / v["image"]))
        if c is None or bgr is None:
            continue
        valid = None
        if v.get("mask"):
            mask = cv2.imread(str(root / "prepared" / v["mask"]), cv2.IMREAD_GRAYSCALE)
            if mask is not None:
                valid = mask <= 127  # 사람 마스크 밖
        cams.append(CheckCamera(v["camera_id"], np.asarray(v["K_undistorted"], float), np.asarray(c["R"], float),
                                np.asarray(c["t"], float).reshape(3), np.ascontiguousarray(bgr[:, :, ::-1]), valid))
    return cams


def cameras_from_raw(calibration: Mapping[str, Any], photos: Mapping[str, Path]) -> list[CheckCamera]:
    """보정 문서 + 카메라별 원본(왜곡 편) 사진. 왜곡은 보정 문서 계수로 편다."""
    import cv2

    from simai_ai.calibration.world_frame.prepare import undistort

    cams: list[CheckCamera] = []
    for c in calibration["cameras"]:
        path = photos.get(c["camera_id"])
        if path is None:
            continue
        bgr = cv2.imread(str(path))
        if bgr is None or list(bgr.shape[1::-1]) != list(c["image_size"]):
            continue
        K = np.asarray(c["K"], float)
        model = c["distortion"]["model"]
        if model == "NONE":
            und, newK = bgr, K
        elif model == "OPENCV_PINHOLE":
            und, newK = undistort(bgr, K, np.asarray(c["distortion"]["coeffs"], float))
        else:
            continue
        cams.append(CheckCamera(c["camera_id"], np.asarray(newK, float), np.asarray(c["R"], float),
                                np.asarray(c["t"], float).reshape(3), np.ascontiguousarray(und[:, :, ::-1])))
    return cams


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GLB 시각 보강(바닥 평면·미관측 색 채우기). 시각용이며 위험 판정에 쓰지 않는다")
    ap.add_argument("--in", dest="src", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path, help="보강 glb. 적용하지 않으면 원본을 그대로 복사한다")
    ap.add_argument("--report", type=Path, help="보강 보고 JSON")
    ap.add_argument("--calibration", type=Path, help="채택 검사용 보정 문서")
    ap.add_argument("--image", action="append", default=[], metavar="CAMERA_ID=PATH", help="채택 검사용 사람 지운 원본 배경 사진")
    ap.add_argument("--time-budget-s", type=float)
    args = ap.parse_args(argv)
    cfg = replace(EnhanceConfig.from_env(), time_budget_s=args.time_budget_s)
    cams = None
    if args.calibration and args.image:
        photos = {k: Path(v) for k, v in (s.split("=", 1) for s in args.image)}
        cams = cameras_from_raw(json.loads(args.calibration.read_text(encoding="utf-8")), photos)
    raw = args.src.read_bytes()
    res = enhance_glb(raw, config=cfg, cameras=cams)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(res.glb if res.glb is not None else raw)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(res.report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: res.report.get(k) for k in ("applied", "reason", "seconds")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
