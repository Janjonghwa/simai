"""노트북 배경 메시(textured.glb) 정리·압축 파이프라인 및 Scene CANDIDATE 등록 도구.

명세 5.3·20.3·13장 기준:
1. Blender 5.2 헤드리스로 부유 조각(floating island) 제거 및 UV 보존 면 수 감축(Decimate COLLAPSE).
2. `@gltf-transform/cli@4.5.1` 로 기하(meshopt) 및 사진 텍스처(KTX2 UASTC/ETC1S) 압축.
3. `SURFACE_SOURCE_MAP` (`surface_source_NNNN.png`) 은 코드 `0·1·2·3` 보존을 위해 무손실 8-bit PNG 유지.
4. 최상위 `extras.simai` 및 머티리얼 속성(`KHR_materials_unlit` 등) 복원 후 리포트 JSON 기록.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

GLB_MAGIC = 0x46546C67
GLB_VERSION = 2
CHUNK_TYPE_JSON = 0x4E4F534A
CHUNK_TYPE_BIN = 0x004E4942
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

GLTF_TRANSFORM_VERSION = "4.5.1"
GLTF_TRANSFORM_PKG = f"@gltf-transform/cli@{GLTF_TRANSFORM_VERSION}"
RIS_KTX2_VERSION = "0.2.0"
RIS_KTX2_PKG = f"ris-ktx2@{RIS_KTX2_VERSION}"

DEFAULT_TARGET_FACES = 400_000
DEFAULT_MIN_ISLAND_RATIO = 0.001
DEFAULT_KTX2_MODE = "uastc"
DEFAULT_TOKEN_ENV_VAR = "SIMAI_ADMIN_TOKEN"


def sha256_file(path: Path) -> str:
    """파일의 SHA-256 해시(hex)를 계산한다."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_repo_root() -> Path:
    """현재 모듈 위치에서 저장소 루트를 찾는다."""
    cur = Path(__file__).resolve()
    for parent in cur.parents:
        if (parent / "ai").is_dir() and (parent / "contracts").is_dir():
            return parent
    return cur.parents[4]


def find_blender_executable(explicit_path: str | Path | None = None) -> Path:
    """Blender 5.2 실행 파일 경로를 찾는다."""
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    env_bin = os.environ.get("BLENDER_BIN")
    if env_bin:
        candidates.append(Path(env_bin))
    which_bin = shutil.which("blender")
    if which_bin:
        candidates.append(Path(which_bin))

    candidates.extend(
        [
            Path(r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"),
            Path(r"C:\Program Files\Blender Foundation\Blender\blender.exe"),
            Path("/Applications/Blender.app/Contents/MacOS/Blender"),
            Path("/usr/bin/blender"),
        ]
    )

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()

    raise FileNotFoundError(
        "Blender 5.2 실행 파일을 찾을 수 없습니다. "
        "PATH 에 blender 를 추가하거나 BLENDER_BIN 환경변수 / --blender 인자를 지정하세요."
    )


def read_glb_json_chunk(glb_path: Path) -> dict[str, Any]:
    """GLB 파일의 JSON 청크를 파싱한다."""
    raw = glb_path.read_bytes()
    if len(raw) < 20:
        raise ValueError(f"GLB 헤더가 너무 짧음: {glb_path}")
    magic, version, _ = struct.unpack_from("<III", raw, 0)
    if magic != GLB_MAGIC or version != GLB_VERSION:
        raise ValueError(f"유효한 GLB 2.0 파일이 아님: {glb_path}")
    chunk_len, chunk_type = struct.unpack_from("<II", raw, 12)
    if chunk_type != CHUNK_TYPE_JSON:
        raise ValueError(f"첫 번째 청크가 JSON 이 아님: {glb_path}")
    return json.loads(raw[20 : 20 + chunk_len].decode("utf-8"))


def count_glb_faces(gltf_json: dict[str, Any]) -> int:
    """GLB JSON 의 accessor 정보를 이용해 총 삼각형 면 수를 계산한다."""
    accessors = gltf_json.get("accessors", [])
    total_faces = 0
    for mesh in gltf_json.get("meshes", []):
        for prim in mesh.get("primitives", []):
            mode = prim.get("mode", 4)
            if mode != 4:
                continue
            if "indices" in prim:
                idx_acc = accessors[prim["indices"]]
                total_faces += int(idx_acc.get("count", 0)) // 3
            else:
                pos_idx = prim.get("attributes", {}).get("POSITION")
                if pos_idx is not None:
                    pos_acc = accessors[pos_idx]
                    total_faces += int(pos_acc.get("count", 0)) // 3
    return total_faces


def inspect_png_header(png_path: Path) -> dict[str, Any]:
    """픽셀을 열지 않고 PNG 파일 헤더(IHDR)만 읽어 8-bit 무손실 여부를 검증한다."""
    with png_path.open("rb") as f:
        header = f.read(33)
    if len(header) < 29 or header[:8] != PNG_SIGNATURE:
        raise ValueError(f"유효한 PNG 시그니처가 아님: {png_path}")
    chunk_type = header[12:16]
    if chunk_type != b"IHDR":
        raise ValueError(f"첫 번째 PNG 청크가 IHDR 가 아님: {png_path}")
    width, height, bit_depth, color_type = struct.unpack_from(">IIBB", header, 16)
    return {
        "width": int(width),
        "height": int(height),
        "bit_depth": int(bit_depth),
        "color_type": int(color_type),
    }


def copy_external_surface_source_maps(
    input_glb: Path,
    output_dir: Path,
    source_json: dict[str, Any],
) -> list[dict[str, Any]]:
    """SURFACE_SOURCE_MAP 텍스처(surface_source_NNNN.png)를 손실 압축 없이 무손실 8-bit PNG로 보존한다."""
    in_dir = input_glb.parent
    candidates: set[Path] = set(in_dir.glob("surface_source_*.png"))

    simai_extras = source_json.get("extras", {}).get("simai", {})
    if isinstance(simai_extras, dict):
        surf = simai_extras.get("surface_source", {})
        if isinstance(surf, dict):
            for mat_entry in surf.get("materials", []):
                if isinstance(mat_entry, dict) and isinstance(
                    mat_entry.get("uri"), str
                ):
                    uri_path = (in_dir / mat_entry["uri"]).resolve()
                    if uri_path.is_file():
                        candidates.add(uri_path)

    preserved: list[dict[str, Any]] = []
    for src_png in sorted(candidates):
        ihdr = inspect_png_header(src_png)
        if ihdr["bit_depth"] != 8:
            raise ValueError(
                f"SURFACE_SOURCE_MAP 은 무손실 8-bit PNG 여야 합니다 "
                f"(실제 bit_depth={ihdr['bit_depth']}): {src_png}"
            )
        dst_png = (output_dir / src_png.name).resolve()
        if dst_png != src_png.resolve():
            shutil.copy2(src_png, dst_png)
        preserved.append(
            {
                "filename": src_png.name,
                "bytes": dst_png.stat().st_size,
                "sha256": sha256_file(dst_png),
                "width": ihdr["width"],
                "height": ihdr["height"],
                "bit_depth": ihdr["bit_depth"],
                "color_type": ihdr["color_type"],
                "encoding": "PNG_LOSSLESS_8BIT",
            }
        )
    return preserved


def preserve_glb_metadata_and_surface_source(
    glb_path: Path,
    source_json: dict[str, Any],
    compression_summary: dict[str, Any],
) -> None:
    """최종 GLB 에 최상위 extras.simai 와 원본 머티리얼 속성(KHR_materials_unlit 등)을 보존한다."""
    raw = glb_path.read_bytes()
    magic, version, _ = struct.unpack_from("<III", raw, 0)
    if magic != GLB_MAGIC or version != GLB_VERSION:
        raise ValueError(f"유효한 GLB 2.0 파일이 아님: {glb_path}")
    json_len, json_type = struct.unpack_from("<II", raw, 12)
    if json_type != CHUNK_TYPE_JSON:
        raise ValueError(f"첫 번째 청크가 JSON 이 아님: {glb_path}")

    gltf = json.loads(raw[20 : 20 + json_len].decode("utf-8"))
    rest = raw[20 + json_len :]

    src_extras = source_json.get("extras")
    merged_extras: dict[str, Any] = (
        copy.deepcopy(src_extras) if isinstance(src_extras, dict) else {}
    )
    if isinstance(gltf.get("extras"), dict):
        for k, v in gltf["extras"].items():
            if k not in merged_extras:
                merged_extras[k] = v

    simai_meta = merged_extras.setdefault("simai", {})
    if isinstance(simai_meta, dict):
        simai_meta["mesh_compression"] = compression_summary
    gltf["extras"] = merged_extras

    src_mats_by_name: dict[str, dict[str, Any]] = {}
    for mat in source_json.get("materials", []):
        if isinstance(mat, dict) and isinstance(mat.get("name"), str):
            src_mats_by_name[mat["name"]] = mat

    has_unlit = False
    for mat in gltf.get("materials", []):
        if not isinstance(mat, dict):
            continue
        name = mat.get("name")
        src_mat = src_mats_by_name.get(name) if isinstance(name, str) else None
        if src_mat is None:
            continue
        for prop in ("doubleSided", "alphaMode", "alphaCutoff", "extras"):
            if prop in src_mat:
                mat[prop] = copy.deepcopy(src_mat[prop])
        if isinstance(src_mat.get("extensions"), dict):
            mat.setdefault("extensions", {}).update(
                copy.deepcopy(src_mat["extensions"])
            )
            if "KHR_materials_unlit" in src_mat["extensions"]:
                has_unlit = True
        src_pbr = src_mat.get("pbrMetallicRoughness")
        if isinstance(src_pbr, dict):
            dst_pbr = mat.setdefault("pbrMetallicRoughness", {})
            for pbr_key in ("baseColorFactor", "roughnessFactor", "metallicFactor"):
                if pbr_key in src_pbr:
                    dst_pbr[pbr_key] = copy.deepcopy(src_pbr[pbr_key])

    if has_unlit:
        ext_used = list(gltf.get("extensionsUsed", []))
        if "KHR_materials_unlit" not in ext_used:
            ext_used.append("KHR_materials_unlit")
        gltf["extensionsUsed"] = ext_used

    new_json = json.dumps(gltf, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    pad = (4 - (len(new_json) % 4)) % 4
    new_json_padded = new_json + (b" " * pad)

    total_len = 12 + 8 + len(new_json_padded) + len(rest)
    header = struct.pack("<III", GLB_MAGIC, GLB_VERSION, total_len)
    json_chunk = (
        struct.pack("<II", len(new_json_padded), CHUNK_TYPE_JSON) + new_json_padded
    )
    glb_path.write_bytes(header + json_chunk + rest)


def _find_npx_executable() -> str:
    for name in ("npx.cmd", "npx"):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        "npx 실행 파일을 찾을 수 없습니다. Node.js 가 설치되어 있는지 확인하세요."
    )


def _find_npm_executable() -> str:
    for name in ("npm.cmd", "npm"):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        "npm 실행 파일을 찾을 수 없습니다. Node.js 가 설치되어 있는지 확인하세요."
    )


def _locate_sharp_in_npx_cache() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates: list[Path] = []
    if local_app_data:
        candidates.append(Path(local_app_data) / "npm-cache" / "_npx")
    home = Path.home()
    candidates.append(home / ".npm" / "_npx")

    for npx_root in candidates:
        if not npx_root.is_dir():
            continue
        for sub in npx_root.iterdir():
            pkg_json = sub / "node_modules" / "@gltf-transform" / "cli" / "package.json"
            sharp_dir = sub / "node_modules" / "sharp"
            if pkg_json.is_file() and sharp_dir.is_dir():
                return pkg_json.resolve()
    return None


def _ensure_ris_ktx2_module() -> Path:
    """WASM 기반 libktx 패키지(ris-ktx2@0.2.0)의 엔트리 경로를 반환한다."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    cache_base = (
        Path(local_app_data) / "simai-ktx-cache"
        if local_app_data
        else Path(tempfile.gettempdir()) / "simai-ktx-cache"
    )
    entry = cache_base / "node_modules" / "ris-ktx2" / "dist" / "index.js"
    if entry.is_file():
        return entry.resolve()

    cache_base.mkdir(parents=True, exist_ok=True)
    npm_bin = _find_npm_executable()
    subprocess.run(
        [npm_bin, "install", "--no-save", "--no-audit", "--no-fund", RIS_KTX2_PKG],
        cwd=str(cache_base),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if not entry.is_file():
        raise RuntimeError(
            f"{RIS_KTX2_PKG} 설치 후 엔트리 파일을 찾을 수 없음: {entry}"
        )
    return entry.resolve()


def _prepare_ktx_shim_env() -> tuple[
    dict[str, str], str, tempfile.TemporaryDirectory[str] | None
]:
    """시스템에 ktx CLI 가 없으면 libktx WASM 기반 ktx 래퍼를 PATH 앞에 추가한다."""
    env = os.environ.copy()
    existing_ktx = shutil.which("ktx")
    if existing_ktx:
        try:
            proc = subprocess.run(
                [existing_ktx, "--version"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return env, f"native ({proc.stdout.strip()})", None
        except OSError:
            pass

    npx_bin = _find_npx_executable()
    sharp_Anchor = _locate_sharp_in_npx_cache()
    if sharp_Anchor is None:
        subprocess.run(
            [npx_bin, "-y", GLTF_TRANSFORM_PKG, "--version"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        sharp_Anchor = _locate_sharp_in_npx_cache()
    if sharp_Anchor is None:
        raise RuntimeError(
            f"{GLTF_TRANSFORM_PKG} 캐시에서 sharp 모듈을 찾을 수 없습니다."
        )

    ris_ktx2_entry = _ensure_ris_ktx2_module()
    scene_mesh_dir = (find_repo_root() / "ai" / "tools" / "scene_mesh").resolve()
    ktx_script = scene_mesh_dir / "ktx_cli.mjs"
    ktx_hook = scene_mesh_dir / "ktx_spawn_hook.mjs"
    if not ktx_script.is_file():
        raise FileNotFoundError(f"ktx_cli.mjs 를 찾을 수 없음: {ktx_script}")
    if not ktx_hook.is_file():
        raise FileNotFoundError(f"ktx_spawn_hook.mjs 를 찾을 수 없음: {ktx_hook}")

    tmp_dir = tempfile.TemporaryDirectory(prefix="simai-ktx-bin-")
    bin_dir = Path(tmp_dir.name)

    cmd_file = bin_dir / "ktx.cmd"
    cmd_file.write_text(
        f'@echo off\r\nnode "{ktx_script}" %*\r\n',
        encoding="utf-8",
    )
    sh_file = bin_dir / "ktx"
    sh_file.write_text(
        f'#!/bin/sh\nexec node "{ktx_script}" "$@"\n',
        encoding="utf-8",
    )
    try:
        sh_file.chmod(0o755)
    except OSError:
        pass

    hook_url = ktx_hook.as_uri()
    existing_node_opts = env.get("NODE_OPTIONS", "").strip()
    import_flag = f"--import={hook_url}"
    env["NODE_OPTIONS"] = f"{existing_node_opts} {import_flag}".strip()
    env["SIMAI_SHARP_MODULE_PATH"] = str(sharp_Anchor)
    env["SIMAI_RIS_KTX2_MODULE_PATH"] = str(ris_ktx2_entry)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env, f"libktx-wasm ({RIS_KTX2_PKG})", tmp_dir


def run_gltf_transform_compress(
    cleaned_glb: Path,
    output_glb: Path,
    *,
    ktx2_mode: str = DEFAULT_KTX2_MODE,
    uastc_level: int = 0,
    zstd_level: int = 3,
    etc1s_quality: int = 128,
) -> dict[str, Any]:
    """`@gltf-transform/cli@4.5.1` 로 사진 텍스처 KTX2 변환 및 기하 meshopt 압축을 실행한다."""
    mode = ktx2_mode.lower().strip()
    if mode not in ("uastc", "etc1s"):
        raise ValueError(
            f"지원하지 않는 ktx2_mode: {ktx2_mode} ('uastc' 또는 'etc1s' 만 허용)"
        )

    npx_bin = _find_npx_executable()
    env, ktx2_backend, tmp_shim = _prepare_ktx_shim_env()

    try:
        with tempfile.TemporaryDirectory(prefix="simai-gltf-transform-") as work_dir:
            ktx2_glb = Path(work_dir) / "step1_ktx2.glb"

            # 1단계: baseColorTexture (사진 아틀라스) 만 KTX2 로 변환
            if mode == "uastc":
                tex_cmd = [
                    npx_bin,
                    "-y",
                    GLTF_TRANSFORM_PKG,
                    "uastc",
                    str(cleaned_glb),
                    str(ktx2_glb),
                    "--slots",
                    "baseColorTexture",
                    "--level",
                    str(uastc_level),
                    "--zstd",
                    str(zstd_level),
                    "--mipmaps",
                    "false",
                    "--jobs",
                    "4",
                ]
            else:
                tex_cmd = [
                    npx_bin,
                    "-y",
                    GLTF_TRANSFORM_PKG,
                    "etc1s",
                    str(cleaned_glb),
                    str(ktx2_glb),
                    "--slots",
                    "baseColorTexture",
                    "--quality",
                    str(etc1s_quality),
                    "--compression",
                    "1",
                    "--mipmaps",
                    "false",
                    "--jobs",
                    "4",
                ]

            proc_tex = subprocess.run(
                tex_cmd,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc_tex.returncode != 0 or not ktx2_glb.is_file():
                raise RuntimeError(
                    f"gltf-transform {mode} 실행 실패 (code={proc_tex.returncode}):\n"
                    f"STDOUT: {proc_tex.stdout}\nSTDERR: {proc_tex.stderr}"
                )

            # 2단계: 기하 meshopt 압축 (전체 씬 단일 양자화 볼륨으로 서브메시 간 경계 크랙 방지)
            meshopt_cmd = [
                npx_bin,
                "-y",
                GLTF_TRANSFORM_PKG,
                "meshopt",
                str(ktx2_glb),
                str(output_glb),
                "--level",
                "high",
                "--quantization-volume",
                "scene",
                "--quantize-position",
                "16",
                "--quantize-texcoord",
                "16",
            ]
            proc_mesh = subprocess.run(
                meshopt_cmd,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc_mesh.returncode != 0 or not output_glb.is_file():
                raise RuntimeError(
                    f"gltf-transform meshopt 실행 실패 (code={proc_mesh.returncode}):\n"
                    f"STDOUT: {proc_mesh.stdout}\nSTDERR: {proc_mesh.stderr}"
                )

            return {
                "gltf_transform_package": GLTF_TRANSFORM_PKG,
                "ktx2_backend": ktx2_backend,
                "ktx2_mode": mode,
                "uastc_level": uastc_level if mode == "uastc" else None,
                "zstd_level": zstd_level if mode == "uastc" else None,
                "etc1s_quality": etc1s_quality if mode == "etc1s" else None,
                "meshopt_level": "high",
                "quantization_volume": "scene",
                "quantize_position_bits": 16,
                "quantize_texcoord_bits": 16,
            }
    finally:
        if tmp_shim is not None:
            tmp_shim.cleanup()


def compress_scene_mesh(
    input_glb: str | Path,
    output_dir: str | Path,
    *,
    target_faces: int = DEFAULT_TARGET_FACES,
    min_island_ratio: float = DEFAULT_MIN_ISLAND_RATIO,
    ktx2_mode: str = DEFAULT_KTX2_MODE,
    uastc_level: int = 0,
    zstd_level: int = 3,
    etc1s_quality: int = 128,
    output_glb_name: str = "textured.glb",
    report_name: str = "report.json",
    blender_bin: str | Path | None = None,
) -> dict[str, Any]:
    """배경 메시 정리(Blender 5.2) 및 압축(@gltf-transform/cli@4.5.1) 전체 파이프라인을 실행한다."""
    in_path = Path(input_glb).resolve()
    if not in_path.is_file():
        raise FileNotFoundError(f"입력 GLB 파일을 찾을 수 없음: {in_path}")

    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_glb_path = (out_dir / output_glb_name).resolve()
    if out_glb_path == in_path:
        raise ValueError(f"입력 파일을 덮어쓸 수 없습니다: {in_path}")

    input_sha256 = sha256_file(in_path)
    bytes_before = in_path.stat().st_size
    source_json = read_glb_json_chunk(in_path)
    faces_before_glb = count_glb_faces(source_json)

    blender_exe = find_blender_executable(blender_bin)
    blender_script = (
        find_repo_root() / "ai" / "tools" / "scene_mesh" / "blender_cleanup.py"
    ).resolve()
    if not blender_script.is_file():
        raise FileNotFoundError(f"blender_cleanup.py 를 찾을 수 없음: {blender_script}")

    with tempfile.TemporaryDirectory(prefix="simai-blender-clean-") as temp_clean_dir:
        temp_clean_path = Path(temp_clean_dir)
        cleaned_glb_path = temp_clean_path / "blender_cleaned.glb"
        blender_report_path = temp_clean_path / "blender_cleanup_report.json"

        blender_cmd = [
            str(blender_exe),
            "-b",
            "--python",
            str(blender_script),
            "--",
            "--in",
            str(in_path),
            "--out",
            str(temp_clean_path),
            "--target-faces",
            str(target_faces),
            "--min-island-ratio",
            str(min_island_ratio),
            "--out-glb",
            cleaned_glb_path.name,
            "--out-report",
            blender_report_path.name,
        ]
        proc_blender = subprocess.run(
            blender_cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc_blender.returncode != 0 or not cleaned_glb_path.is_file():
            raise RuntimeError(
                f"Blender 5.2 헤드리스 정리 실패 (code={proc_blender.returncode}):\n"
                f"STDOUT: {proc_blender.stdout}\nSTDERR: {proc_blender.stderr}"
            )

        blender_report = json.loads(blender_report_path.read_text(encoding="utf-8"))
        bytes_after_blender = cleaned_glb_path.stat().st_size

        gltf_transform_info = run_gltf_transform_compress(
            cleaned_glb_path,
            out_glb_path,
            ktx2_mode=ktx2_mode,
            uastc_level=uastc_level,
            zstd_level=zstd_level,
            etc1s_quality=etc1s_quality,
        )

    compression_meta = {
        "pipeline": "blender_5.2_cleanup+gltf_transform_meshopt_ktx2",
        "blender_version": blender_report["blender_version"],
        "gltf_transform_package": gltf_transform_info["gltf_transform_package"],
        "ktx2_mode": gltf_transform_info["ktx2_mode"],
        "target_faces": target_faces,
        "min_island_ratio": min_island_ratio,
        "faces_before": faces_before_glb,
        "faces_after": blender_report["faces_after"],
        "islands_removed": blender_report["islands_removed"],
    }
    preserve_glb_metadata_and_surface_source(
        out_glb_path, source_json, compression_meta
    )
    surface_source_maps = copy_external_surface_source_maps(
        in_path, out_dir, source_json
    )

    output_sha256 = sha256_file(out_glb_path)
    bytes_after = out_glb_path.stat().st_size
    final_json = read_glb_json_chunk(out_glb_path)
    faces_after_glb = count_glb_faces(final_json)

    report: dict[str, Any] = {
        "input_path": str(in_path),
        "output_path": str(out_glb_path),
        "input_sha256": input_sha256,
        "output_sha256": output_sha256,
        "faces_before": faces_before_glb,
        "faces_after_island_removal": blender_report["faces_after_island_removal"],
        "faces_after": faces_after_glb,
        "islands_total": blender_report["islands_total"],
        "islands_removed": blender_report["islands_removed"],
        "islands_kept": blender_report["islands_kept"],
        "removed_faces": blender_report["removed_faces"],
        "removed_faces_ratio": blender_report["removed_faces_ratio"],
        "total_area_before_m2": blender_report["total_area_before_m2"],
        "total_area_after_m2": blender_report["total_area_after_m2"],
        "removed_area_m2": blender_report["removed_area_m2"],
        "removed_area_ratio": blender_report["removed_area_ratio"],
        "bytes_before": bytes_before,
        "bytes_after_blender": bytes_after_blender,
        "bytes_after": bytes_after,
        "size_reduction_ratio": round(1.0 - (bytes_after / max(1, bytes_before)), 6),
        "extensions_used": final_json.get("extensionsUsed", []),
        "surface_source_maps": surface_source_maps,
        "tools": {
            "blender_executable": str(blender_exe),
            "blender_version": blender_report["blender_version"],
            "gltf_transform_package": gltf_transform_info["gltf_transform_package"],
            "ktx2_backend": gltf_transform_info["ktx2_backend"],
        },
        "parameters": {
            "target_faces": target_faces,
            "min_island_ratio": min_island_ratio,
            "decimate_skipped": blender_report["decimate_skipped"],
            "decimate_ratio": blender_report["decimate_ratio"],
            "ktx2_mode": gltf_transform_info["ktx2_mode"],
            "uastc_level": gltf_transform_info["uastc_level"],
            "zstd_level": gltf_transform_info["zstd_level"],
            "etc1s_quality": gltf_transform_info["etc1s_quality"],
            "quantization_volume": gltf_transform_info["quantization_volume"],
            "quantize_position_bits": gltf_transform_info["quantize_position_bits"],
            "quantize_texcoord_bits": gltf_transform_info["quantize_texcoord_bits"],
        },
    }

    report_path = (out_dir / report_name).resolve()
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def register_scene_candidate(
    *,
    server_url: str,
    site_id: str,
    aligned_to_calibration_id: str,
    glb_path: str | Path,
    report: dict[str, Any],
    token_env_var: str = DEFAULT_TOKEN_ENV_VAR,
) -> dict[str, Any]:
    """관리자 API(/api/uploads/sign -> PUT -> /api/scenes)로 새 Scene 버전(CANDIDATE)을 등록한다.

    관리자 토큰은 지정된 환경변수에서만 읽으며, 로그나 예외 메시지에 토큰·서명 URL을 절대 출력하지 않는다.
    """
    token = os.environ.get(token_env_var, "").strip()
    if not token:
        raise RuntimeError(
            f"관리자 토큰 환경변수({token_env_var})가 비어 있습니다. "
            "토큰은 환경변수로만 주입해야 합니다."
        )

    resolved_glb = Path(glb_path).resolve()
    if not resolved_glb.is_file():
        raise FileNotFoundError(f"업로드할 GLB 파일을 찾을 수 없음: {resolved_glb}")

    glb_bytes = resolved_glb.read_bytes()
    base_url = server_url.rstrip("/")
    auth_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # 1. POST /api/uploads/sign
    sign_payload = json.dumps(
        {
            "purpose": "SCENE_GLTF",
            "contentType": "model/gltf-binary",
            "sizeBytes": len(glb_bytes),
        }
    ).encode("utf-8")
    sign_req = urllib.request.Request(
        f"{base_url}/api/uploads/sign",
        data=sign_payload,
        headers=auth_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(sign_req, timeout=30) as resp:
            sign_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"POST /api/uploads/sign 실패 (HTTP {exc.code})") from None

    upload_url = sign_data.get("uploadUrl")
    object_key = sign_data.get("objectKey")
    if not isinstance(upload_url, str) or not isinstance(object_key, str):
        raise TypeError(
            "POST /api/uploads/sign 응답에 uploadUrl 또는 objectKey 가 없습니다."
        )

    # 2. PUT <uploadUrl> (서명 URL 자체는 로그에 출력하지 않음)
    put_req = urllib.request.Request(
        upload_url,
        data=glb_bytes,
        headers={"Content-Type": "model/gltf-binary"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(put_req, timeout=120) as resp:
            if resp.status not in (200, 201, 204):
                raise RuntimeError(f"S3 PUT 업로드 실패 (HTTP {resp.status})")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"S3 PUT 업로드 실패 (HTTP {exc.code})") from None

    # 3. POST /api/scenes (CANDIDATE 등록)
    quality_summary = {
        "input_sha256": report.get("input_sha256"),
        "output_sha256": report.get("output_sha256"),
        "faces_before": report.get("faces_before"),
        "faces_after": report.get("faces_after"),
        "islands_removed": report.get("islands_removed"),
        "bytes_before": report.get("bytes_before"),
        "bytes_after": report.get("bytes_after"),
        "extensions_used": report.get("extensions_used"),
        "parameters": report.get("parameters"),
    }
    scene_payload = json.dumps(
        {
            "siteId": site_id,
            "alignedToCalibrationId": aligned_to_calibration_id,
            "gltfAssetKey": object_key,
            "qualitySummaryJson": json.dumps(quality_summary, ensure_ascii=False),
        }
    ).encode("utf-8")
    scene_req = urllib.request.Request(
        f"{base_url}/api/scenes",
        data=scene_payload,
        headers=auth_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(scene_req, timeout=30) as resp:
            scene_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"POST /api/scenes 실패 (HTTP {exc.code})") from None

    return {
        "sceneId": scene_data.get("sceneId") or scene_data.get("id"),
        "status": scene_data.get("status", "CANDIDATE"),
        "gltfAssetKey": object_key,
        "response": scene_data,
    }
