"""배경 메시 정리·압축 도구(simai_ai.scene.mesh_compress) 단위·통합 시험."""

from __future__ import annotations

import io
import json
import struct
import urllib.error
import zlib
from pathlib import Path
from typing import Any, Self

import pytest
from simai_ai.scene.mesh_compress import (
    CHUNK_TYPE_BIN,
    CHUNK_TYPE_JSON,
    GLB_MAGIC,
    GLB_VERSION,
    compress_scene_mesh,
    count_glb_faces,
    find_blender_executable,
    inspect_png_header,
    read_glb_json_chunk,
    register_scene_candidate,
    sha256_file,
)


def _blender_available() -> bool:
    try:
        find_blender_executable()
    except FileNotFoundError:
        return False
    return True


# 실제 Blender 5.2 로 정리·Decimate 하는 통합 시험. Blender 가 없는 CI(Jenkins)에서는 건너뛴다
needs_blender = pytest.mark.skipif(not _blender_available(), reason="Blender 5.2 실행 파일 없음")


def _make_minimal_png_8bit(
    width: int = 8, height: int = 8, gray_value: int = 2
) -> bytes:
    """외부 이미지 라이브러리 없이 유효한 무손실 8-bit PNG 바이트를 생성한다."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    # Color type 2 (RGB, 8-bit) so both photo texture and 8-bit header checks pass
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_rows = b"".join(
        b"\x00" + bytes([gray_value, gray_value, gray_value]) * width
        for _ in range(height)
    )
    idat = zlib.compress(raw_rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def _build_synthetic_textured_glb(
    glb_path: Path,
    *,
    grid_n: int = 10,
    include_floating_island: bool = True,
) -> dict[str, Any]:
    """시험용 합성 textured.glb 를 생성한다.

    - 메인 바닥 격자: `grid_n x grid_n` 칸 = `2 * grid_n^2` 개 삼각형 (예: 10x10 -> 200 faces, 면적 100 m^2)
    - 부유 조각(floating island): 공중에 떨어진 1개 미세 삼각형 (면적 0.005 m^2)
    - `observed_0000` 머티리얼(8x8 PNG 텍스처 + `KHR_materials_unlit`) 및 `unobserved_neutral` 머티리얼 포함
    - 최상위 `extras.simai` 메타데이터 포함
    """
    positions: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    indices_obs: list[int] = []

    for iy in range(grid_n + 1):
        for ix in range(grid_n + 1):
            positions.append((float(ix), 0.0, float(iy)))
            uvs.append((ix / float(grid_n), iy / float(grid_n)))

    stride = grid_n + 1
    for iy in range(grid_n):
        for ix in range(grid_n):
            v00 = iy * stride + ix
            v10 = v00 + 1
            v01 = (iy + 1) * stride + ix
            v11 = v01 + 1
            indices_obs.extend([v00, v01, v10, v10, v01, v11])

    if include_floating_island:
        base_idx = len(positions)
        positions.extend(
            [
                (50.0, 25.0, 50.0),
                (50.1, 25.0, 50.0),
                (50.0, 25.1, 50.0),
            ]
        )
        uvs.extend([(0.1, 0.1), (0.2, 0.1), (0.1, 0.2)])
        indices_obs.extend([base_idx, base_idx + 1, base_idx + 2])

    # unobserved_neutral 보조 메시 (메인 바닥 옆에 붙어 있는 2개 삼각형)
    unobs_positions = [
        (float(grid_n), 0.0, 0.0),
        (float(grid_n + 1), 0.0, 0.0),
        (float(grid_n), 0.0, 1.0),
        (float(grid_n + 1), 0.0, 1.0),
    ]
    unobs_indices = [0, 2, 1, 1, 2, 3]

    pos_bytes = b"".join(struct.pack("<fff", *p) for p in positions)
    uv_bytes = b"".join(struct.pack("<ff", *uv) for uv in uvs)
    idx_bytes = b"".join(struct.pack("<I", idx) for idx in indices_obs)

    unobs_pos_bytes = b"".join(struct.pack("<fff", *p) for p in unobs_positions)
    unobs_idx_bytes = b"".join(struct.pack("<I", idx) for idx in unobs_indices)
    png_bytes = _make_minimal_png_8bit(8, 8, 128)

    def _align4(buf: bytes) -> bytes:
        pad = (4 - (len(buf) % 4)) % 4
        return buf + (b"\x00" * pad)

    chunks: list[bytes] = []
    buffer_views: list[dict[str, Any]] = []

    def _add_bv(data: bytes, target: int | None = None) -> int:
        offset = sum(len(c) for c in chunks)
        bv: dict[str, Any] = {
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(data),
        }
        if target is not None:
            bv["target"] = target
        buffer_views.append(bv)
        chunks.append(_align4(data))
        return len(buffer_views) - 1

    bv_idx = _add_bv(idx_bytes, 34963)
    bv_pos = _add_bv(pos_bytes, 34962)
    bv_uv = _add_bv(uv_bytes, 34962)
    bv_unobs_idx = _add_bv(unobs_idx_bytes, 34963)
    bv_unobs_pos = _add_bv(unobs_pos_bytes, 34962)
    bv_png = _add_bv(png_bytes)

    bin_blob = b"".join(chunks)

    pos_xs = [p[0] for p in positions]
    pos_ys = [p[1] for p in positions]
    pos_zs = [p[2] for p in positions]

    unobs_xs = [p[0] for p in unobs_positions]
    unobs_ys = [p[1] for p in unobs_positions]
    unobs_zs = [p[2] for p in unobs_positions]

    gltf_json: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "simai-test-synth"},
        "extensionsUsed": ["KHR_materials_unlit"],
        "scene": 0,
        "scenes": [{"nodes": [0, 1]}],
        "nodes": [
            {"name": "textured_0000", "mesh": 0},
            {"name": "unobserved_neutral", "mesh": 1},
        ],
        "meshes": [
            {
                "name": "textured_0000",
                "primitives": [
                    {
                        "attributes": {"POSITION": 1, "TEXCOORD_0": 2},
                        "indices": 0,
                        "material": 0,
                        "mode": 4,
                    }
                ],
            },
            {
                "name": "unobserved_neutral",
                "primitives": [
                    {
                        "attributes": {"POSITION": 4},
                        "indices": 3,
                        "material": 1,
                        "mode": 4,
                    }
                ],
            },
        ],
        "materials": [
            {
                "name": "observed_0000",
                "doubleSided": False,
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": 0},
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
                "extensions": {"KHR_materials_unlit": {}},
            },
            {
                "name": "unobserved_neutral",
                "doubleSided": False,
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.5, 0.5, 0.5, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
                "extensions": {"KHR_materials_unlit": {}},
            },
        ],
        "textures": [{"source": 0}],
        "images": [
            {"bufferView": bv_png, "mimeType": "image/png", "name": "atlas_0000"}
        ],
        "accessors": [
            {
                "bufferView": bv_idx,
                "componentType": 5125,
                "count": len(indices_obs),
                "type": "SCALAR",
            },
            {
                "bufferView": bv_pos,
                "componentType": 5126,
                "count": len(positions),
                "type": "VEC3",
                "min": [min(pos_xs), min(pos_ys), min(pos_zs)],
                "max": [max(pos_xs), max(pos_ys), max(pos_zs)],
            },
            {
                "bufferView": bv_uv,
                "componentType": 5126,
                "count": len(uvs),
                "type": "VEC2",
            },
            {
                "bufferView": bv_unobs_idx,
                "componentType": 5125,
                "count": len(unobs_indices),
                "type": "SCALAR",
            },
            {
                "bufferView": bv_unobs_pos,
                "componentType": 5126,
                "count": len(unobs_positions),
                "type": "VEC3",
                "min": [min(unobs_xs), min(unobs_ys), min(unobs_zs)],
                "max": [max(unobs_xs), max(unobs_ys), max(unobs_zs)],
            },
        ],
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_blob)}],
        "extras": {
            "simai": {
                "format": "simai.scene_gltf_extras.v1",
                "axis_convention": "GLTF_Y_UP",
                "surface_source": {
                    "encoding": "SURFACE_SOURCE_MAP",
                    "codes": {
                        "0": "UNOBSERVED",
                        "1": "OBSERVED",
                        "2": "INFERRED",
                        "3": "SIM",
                    },
                    "materials": [
                        {
                            "material_index": 0,
                            "uri": "surface_source_0000.png",
                            "counts": {
                                "UNOBSERVED": 0,
                                "OBSERVED": 64,
                                "INFERRED": 0,
                                "SIM": 0,
                            },
                        }
                    ],
                },
                "unobserved": {
                    "policy": "NEUTRAL_SURFACE",
                    "material_name": "unobserved_neutral",
                    "rgba": [128, 128, 128, 255],
                },
            }
        },
    }

    json_bytes = json.dumps(gltf_json, separators=(",", ":")).encode("utf-8")
    json_pad = (4 - (len(json_bytes) % 4)) % 4
    json_padded = json_bytes + (b" " * json_pad)

    total_len = 12 + 8 + len(json_padded) + 8 + len(bin_blob)
    glb_bytes = (
        struct.pack("<III", GLB_MAGIC, GLB_VERSION, total_len)
        + struct.pack("<II", len(json_padded), CHUNK_TYPE_JSON)
        + json_padded
        + struct.pack("<II", len(bin_blob), CHUNK_TYPE_BIN)
        + bin_blob
    )
    glb_path.write_bytes(glb_bytes)
    return gltf_json


@needs_blender
def test_compress_scene_mesh_removes_floating_islands_and_preserves_surface_source(
    tmp_path: Path,
) -> None:
    in_dir = tmp_path / "input"
    out_dir = tmp_path / "output"
    in_dir.mkdir()

    in_glb = in_dir / "textured.glb"
    _build_synthetic_textured_glb(in_glb, grid_n=10, include_floating_island=True)

    # SURFACE_SOURCE_MAP 무손실 8-bit PNG (코드 값 1 저장)
    surf_png = in_dir / "surface_source_0000.png"
    surf_png.write_bytes(_make_minimal_png_8bit(8, 8, 1))
    orig_surf_sha256 = sha256_file(surf_png)

    # 1. target_faces=100 으로 실행 -> 부유 조각 1개 제거 + UV 보존 Decimate 동시 검증
    report = compress_scene_mesh(
        in_glb,
        out_dir,
        target_faces=100,
        min_island_ratio=0.02,
        ktx2_mode="uastc",
    )

    out_glb = out_dir / "textured.glb"
    assert out_glb.is_file()
    assert (out_dir / "report.json").is_file()

    # 입력 파일이 수정되지 않았는지 확인
    assert report["input_sha256"] == sha256_file(in_glb)
    assert report["output_sha256"] == sha256_file(out_glb)
    assert report["faces_before"] == 203  # 200 (main) + 1 (floating) + 2 (unobserved)
    assert report["islands_total"] == 2
    assert report["islands_removed"] == 1
    assert report["removed_faces"] == 1
    assert report["faces_after_island_removal"] == 202
    assert report["parameters"]["decimate_skipped"] is False
    assert report["faces_after"] <= 105

    # 출력 GLB 확장 및 메타데이터 확인
    out_json = read_glb_json_chunk(out_glb)
    assert count_glb_faces(out_json) == report["faces_after"]
    ext_used = set(out_json.get("extensionsUsed", []))
    assert "EXT_meshopt_compression" in ext_used
    assert "KHR_texture_basisu" in ext_used
    assert "KHR_materials_unlit" in ext_used

    simai_extras = out_json.get("extras", {}).get("simai", {})
    assert simai_extras.get("axis_convention") == "GLTF_Y_UP"
    assert (
        simai_extras.get("surface_source", {}).get("encoding") == "SURFACE_SOURCE_MAP"
    )
    assert (
        simai_extras.get("unobserved", {}).get("material_name") == "unobserved_neutral"
    )
    assert simai_extras.get("mesh_compression", {}).get("islands_removed") == 1

    # SURFACE_SOURCE_MAP 은 손실 압축되지 않고 무손실 8-bit PNG 그대로 유지되어야 함
    out_surf_png = out_dir / "surface_source_0000.png"
    assert out_surf_png.is_file()
    assert sha256_file(out_surf_png) == orig_surf_sha256
    ihdr = inspect_png_header(out_surf_png)
    assert ihdr["bit_depth"] == 8
    assert len(report["surface_source_maps"]) == 1
    assert report["surface_source_maps"][0]["encoding"] == "PNG_LOSSLESS_8BIT"


@needs_blender
def test_compress_scene_mesh_skips_decimate_when_already_below_target(
    tmp_path: Path,
) -> None:
    in_dir = tmp_path / "in_skip"
    out_dir = tmp_path / "out_skip"
    in_dir.mkdir()

    in_glb = in_dir / "textured.glb"
    _build_synthetic_textured_glb(in_glb, grid_n=4, include_floating_island=False)

    report = compress_scene_mesh(
        in_glb,
        out_dir,
        target_faces=400_000,
        min_island_ratio=0.001,
        ktx2_mode="uastc",
    )
    assert report["parameters"]["decimate_skipped"] is True
    assert report["faces_before"] == 34
    assert report["faces_after"] == 34

    # 입력 파일과 동일한 경로로 출력 시도 시 거부 검증
    with pytest.raises(ValueError, match="덮어쓸 수 없습니다"):
        compress_scene_mesh(in_glb, in_dir, output_glb_name="textured.glb")


def test_register_scene_candidate_uses_env_token_and_never_leaks_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glb_path = tmp_path / "textured.glb"
    _build_synthetic_textured_glb(glb_path, grid_n=2, include_floating_island=False)
    fake_report = {
        "input_sha256": "aaa",
        "output_sha256": "bbb",
        "faces_before": 10,
        "faces_after": 10,
        "islands_removed": 0,
        "bytes_before": 100,
        "bytes_after": 50,
        "extensions_used": ["EXT_meshopt_compression"],
        "parameters": {"target_faces": 400000},
    }

    # 1. 환경변수 미설정 시 실패
    monkeypatch.delenv("SIMAI_ADMIN_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="SIMAI_ADMIN_TOKEN"):
        register_scene_candidate(
            server_url="https://simai.live",
            site_id="site-1",
            aligned_to_calibration_id="cal-1",
            glb_path=glb_path,
            report=fake_report,
        )

    # 2. 정상 3단계 호출 (POST /api/uploads/sign -> PUT -> POST /api/scenes)
    secret_token = "super-secret-admin-jwt-token-do-not-print"
    secret_presigned_url = "https://s3.ap-northeast-2.amazonaws.com/bucket/scene.glb?X-Amz-Signature=secret123"
    monkeypatch.setenv("SIMAI_ADMIN_TOKEN", secret_token)

    recorded_requests: list[tuple[str, str, dict[str, str]]] = []

    class _FakeResponse:
        def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
            self.status = status
            self._raw = json.dumps(body or {}).encode("utf-8")

        def read(self) -> bytes:
            return self._raw

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    def _fake_urlopen(
        req: urllib.request.Request, timeout: float = 30
    ) -> _FakeResponse:
        headers = {k.lower(): v for k, v in req.header_items()}
        recorded_requests.append((req.get_method(), req.full_url, headers))
        if req.full_url.endswith("/api/uploads/sign"):
            assert headers.get("authorization") == f"Bearer {secret_token}"
            return _FakeResponse(
                200,
                {
                    "uploadUrl": secret_presigned_url,
                    "objectKey": "scenes/site-1/textured.glb",
                },
            )
        if req.full_url == secret_presigned_url:
            assert "authorization" not in headers
            return _FakeResponse(200, {})
        if req.full_url.endswith("/api/scenes"):
            assert headers.get("authorization") == f"Bearer {secret_token}"
            return _FakeResponse(
                201,
                {"sceneId": "scene-uuid-404", "status": "CANDIDATE"},
            )
        raise AssertionError(f"Unexpected URL: {req.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    res = register_scene_candidate(
        server_url="https://simai.live/",
        site_id="site-1",
        aligned_to_calibration_id="cal-1",
        glb_path=glb_path,
        report=fake_report,
    )
    assert res["sceneId"] == "scene-uuid-404"
    assert res["status"] == "CANDIDATE"
    assert res["gltfAssetKey"] == "scenes/site-1/textured.glb"
    assert len(recorded_requests) == 3

    # 3. PUT 실패 시 예외 메시지에 서명 URL이나 토큰이 노출되지 않는지 확인
    def _failing_urlopen(
        req: urllib.request.Request, timeout: float = 30
    ) -> _FakeResponse:
        if req.full_url.endswith("/api/uploads/sign"):
            return _FakeResponse(
                200,
                {
                    "uploadUrl": secret_presigned_url,
                    "objectKey": "scenes/site-1/textured.glb",
                },
            )
        raise urllib.error.HTTPError(
            url=secret_presigned_url,
            code=403,
            msg="Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"AccessDenied"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", _failing_urlopen)
    with pytest.raises(RuntimeError) as exc_info:
        register_scene_candidate(
            server_url="https://simai.live",
            site_id="site-1",
            aligned_to_calibration_id="cal-1",
            glb_path=glb_path,
            report=fake_report,
        )
    err_text = str(exc_info.value)
    assert secret_token not in err_text
    assert "X-Amz-Signature" not in err_text
