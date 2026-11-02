"""Blender 5.2 헤드리스 배경 메시 정리 스크립트.

사용 예시:
  blender -b --python ai/tools/scene_mesh/blender_cleanup.py -- \\
    --in C:/Users/SSAFY/simai-local/session9/scene-3ce8c02f/textured.glb \\
    --out C:/Users/SSAFY/simai-local/scene-mesh-404 \\
    --target-faces 400000 \\
    --min-island-ratio 0.001
"""

from __future__ import annotations

import argparse
import copy
import json
import struct
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

GLB_MAGIC = 0x46546C67
GLB_VERSION = 2
CHUNK_TYPE_JSON = 0x4E4F534A
CHUNK_TYPE_BIN = 0x004E4942

DEFAULT_TARGET_FACES = 400_000
DEFAULT_MIN_Island_RATIO = 0.001
WELD_DISTANCE_M = 1e-5
COORD_ROUND_DIGITS = 5


def read_glb_json(glb_path: Path) -> dict[str, Any]:
    """GLB 파일의 JSON 청크를 파싱해 반환한다."""
    raw = glb_path.read_bytes()
    if len(raw) < 20:
        raise ValueError(f"GLB 헤더가 너무 짧음: {glb_path}")
    magic, version, _total_len = struct.unpack_from("<III", raw, 0)
    if magic != GLB_MAGIC or version != GLB_VERSION:
        raise ValueError(f"유효한 GLB 2.0 파일이 아님: {glb_path}")
    chunk_len, chunk_type = struct.unpack_from("<II", raw, 12)
    if chunk_type != CHUNK_TYPE_JSON:
        raise ValueError(f"첫 번째 청크가 JSON 이 아님: {glb_path}")
    json_bytes = raw[20 : 20 + chunk_len]
    return json.loads(json_bytes.decode("utf-8"))


def patch_glb_json_metadata(
    glb_path: Path,
    source_json: dict[str, Any],
) -> None:
    """익스포트된 GLB에 원본 최상위 extras.simai 및 머티리얼 속성(unlit 등)을 복원한다."""
    raw = glb_path.read_bytes()
    magic, version, _ = struct.unpack_from("<III", raw, 0)
    if magic != GLB_MAGIC or version != GLB_VERSION:
        return
    json_len, json_type = struct.unpack_from("<II", raw, 12)
    if json_type != CHUNK_TYPE_JSON:
        return

    gltf = json.loads(raw[20 : 20 + json_len].decode("utf-8"))
    rest = raw[20 + json_len :]

    src_extras = source_json.get("extras")
    if isinstance(src_extras, dict):
        merged_extras = copy.deepcopy(src_extras)
        if isinstance(gltf.get("extras"), dict):
            for k, v in gltf["extras"].items():
                if k not in merged_extras:
                    merged_extras[k] = v
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


def has_normal_attribute(gltf_json: dict[str, Any]) -> bool:
    """원본 GLB 프리미티브에 NORMAL 정점 속성이 있는지 확인한다."""
    for mesh in gltf_json.get("meshes", []):
        for prim in mesh.get("primitives", []):
            attrs = prim.get("attributes", {})
            if "NORMAL" in attrs:
                return True
    return False


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Blender 5.2 헤드리스 배경 메시 부유 조각 제거 및 UV 보존 면 수 감축 도구",
    )
    parser.add_argument("--in", dest="input_glb", required=True, help="입력 .glb 경로")
    parser.add_argument(
        "--out", dest="output_dir", required=True, help="출력 디렉터리 경로"
    )
    parser.add_argument(
        "--target-faces",
        type=int,
        default=DEFAULT_TARGET_FACES,
        help=f"Decimate 목표 면 수 (기본값: {DEFAULT_TARGET_FACES})",
    )
    parser.add_argument(
        "--min-island-ratio",
        type=float,
        default=DEFAULT_MIN_Island_RATIO,
        help=(
            "전체 면 수 및 면적 대비 부유 조각 제거 최소 비율 "
            f"(기본값: {DEFAULT_MIN_Island_RATIO})"
        ),
    )
    parser.add_argument(
        "--out-glb",
        default="blender_cleaned.glb",
        help="출력 디렉터리 내 정리된 GLB 파일명 (기본값: blender_cleaned.glb)",
    )
    parser.add_argument(
        "--out-report",
        default="blender_cleanup_report.json",
        help="출력 디렉터리 내 Blender 정리 리포트 JSON 파일명",
    )
    return parser.parse_args(argv)


def run_blender_cleanup(args: argparse.Namespace) -> dict[str, Any]:
    import bmesh  # type: ignore[import-not-found]
    import bpy  # type: ignore[import-not-found]

    in_path = Path(args.input_glb).resolve()
    if not in_path.is_file():
        raise FileNotFoundError(f"입력 GLB 파일을 찾을 수 없음: {in_path}")

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_glb_path = (out_dir / args.out_glb).resolve()
    if out_glb_path == in_path:
        out_glb_path = (out_dir / f"{in_path.stem}.cleaned.glb").resolve()

    if args.target_faces < 4:
        raise ValueError(f"--target-faces 는 4 이상이어야 함: {args.target_faces}")
    if not 0.0 <= args.min_island_ratio < 1.0:
        raise ValueError(
            f"--min-island-ratio 는 [0.0, 1.0) 범위여야 함: {args.min_island_ratio}"
        )

    source_json = read_glb_json(in_path)
    export_normals = has_normal_attribute(source_json)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(in_path), merge_vertices=True)

    mesh_objs = [o for o in bpy.data.objects if o.type == "MESH"]
    if not mesh_objs:
        raise RuntimeError(f"임포트된 메시 오브젝트가 없음: {in_path}")

    raw_imported_faces = sum(len(o.data.polygons) for o in mesh_objs)

    # 1. 각 메시 오브젝트 내 1e-5 m 이내 중복 정점 병합 (루프별 UV 및 머티리얼 보존)
    for obj in mesh_objs:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=WELD_DISTANCE_M)
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()

    # 2. 전체 메시 오브젝트 통합 3D 공간 좌표 기준 연결 성분(island) 계산 (O(N))
    pos_to_faces: dict[tuple[float, float, float], list[int]] = defaultdict(list)
    face_records: list[
        tuple[int, int, float, tuple[tuple[float, float, float], ...]]
    ] = []
    total_faces_before = 0
    total_area_before = 0.0

    for obj_idx, obj in enumerate(mesh_objs):
        mw = obj.matrix_world
        mesh = obj.data
        world_verts = [mw @ v.co for v in mesh.vertices]
        for poly in mesh.polygons:
            fid = len(face_records)
            area = float(poly.area)
            keys = tuple(
                (
                    round(world_verts[vi].x, COORD_ROUND_DIGITS),
                    round(world_verts[vi].y, COORD_ROUND_DIGITS),
                    round(world_verts[vi].z, COORD_ROUND_DIGITS),
                )
                for vi in poly.vertices
            )
            face_records.append((obj_idx, poly.index, area, keys))
            total_faces_before += 1
            total_area_before += area
            for k in keys:
                pos_to_faces[k].append(fid)

    visited = [False] * len(face_records)
    islands: list[tuple[list[int], float]] = []

    for start_fid in range(len(face_records)):
        if visited[start_fid]:
            continue
        queue: deque[int] = deque([start_fid])
        visited[start_fid] = True
        comp_fids: list[int] = []
        comp_area = 0.0
        while queue:
            cur = queue.popleft()
            comp_fids.append(cur)
            _obj_idx, _p_idx, area, keys = face_records[cur]
            comp_area += area
            for k in keys:
                neighbors = pos_to_faces.pop(k, ())
                for nb in neighbors:
                    if not visited[nb]:
                        visited[nb] = True
                        queue.append(nb)
        islands.append((comp_fids, comp_area))

    # 가장 큰 연결 성분은 어떤 경우에도 제거하지 않도록 보호
    largest_island_idx = (
        max(
            range(len(islands)), key=lambda idx: (len(islands[idx][0]), islands[idx][1])
        )
        if islands
        else -1
    )

    faces_to_remove_by_obj: dict[int, set[int]] = defaultdict(set)
    removed_islands = 0
    removed_faces = 0
    removed_area = 0.0

    if args.min_island_ratio > 0.0 and len(islands) > 1:
        for idx, (comp_fids, comp_area) in enumerate(islands):
            if idx == largest_island_idx:
                continue
            face_ratio = len(comp_fids) / max(1, total_faces_before)
            area_ratio = comp_area / max(1e-12, total_area_before)
            if (
                face_ratio < args.min_island_ratio
                and area_ratio < args.min_island_ratio
            ):
                removed_islands += 1
                removed_faces += len(comp_fids)
                removed_area += comp_area
                for fid in comp_fids:
                    obj_idx, p_idx, _area, _keys = face_records[fid]
                    faces_to_remove_by_obj[obj_idx].add(p_idx)

    for obj_idx, rm_indices in faces_to_remove_by_obj.items():
        if not rm_indices:
            continue
        obj = mesh_objs[obj_idx]
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        target_bm_faces = [bm.faces[i] for i in rm_indices if i < len(bm.faces)]
        if target_bm_faces:
            bmesh.ops.delete(bm, geom=target_bm_faces, context="FACES")
        loose_edges = [e for e in bm.edges if not e.link_faces]
        if loose_edges:
            bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
        loose_verts = [v for v in bm.verts if not v.link_faces]
        if loose_verts:
            bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()

    # 빈 메시 오브젝트 정리
    for obj in list(mesh_objs):
        if len(obj.data.polygons) == 0:
            bpy.data.objects.remove(obj, do_unlink=True)

    mesh_objs = [o for o in bpy.data.objects if o.type == "MESH"]
    faces_after_islands = sum(len(o.data.polygons) for o in mesh_objs)

    # 3. UV 보존 면 수 감축 (Decimate COLLAPSE, 이미 target_faces 이하이면 생략)
    decimate_skipped = faces_after_islands <= args.target_faces
    decimate_ratio = 1.0
    if not decimate_skipped and faces_after_islands > 0:
        decimate_ratio = args.target_faces / float(faces_after_islands)
        for obj in mesh_objs:
            if len(obj.data.polygons) < 4:
                continue
            bpy.context.view_layer.objects.active = obj
            mod = obj.modifiers.new(name="SimaiDecimateCollapse", type="DECIMATE")
            mod.decimate_type = "COLLAPSE"
            mod.ratio = decimate_ratio
            mod.delimit = {"UV", "MATERIAL", "SEAM"}
            bpy.ops.object.modifier_apply(modifier=mod.name)

    faces_after_decimate = sum(len(o.data.polygons) for o in mesh_objs)
    total_area_after = sum(float(p.area) for o in mesh_objs for p in o.data.polygons)

    # 4. GLB 익스포트 및 메타데이터(extras.simai, 머티리얼 속성) 복원
    bpy.ops.export_scene.gltf(
        filepath=str(out_glb_path),
        export_format="GLB",
        export_yup=True,
        export_texcoords=True,
        export_normals=export_normals,
        export_materials="EXPORT",
        export_extras=True,
    )
    patch_glb_json_metadata(out_glb_path, source_json)

    report: dict[str, Any] = {
        "blender_version": bpy.app.version_string,
        "input_glb": str(in_path),
        "output_glb": str(out_glb_path),
        "target_faces": args.target_faces,
        "min_island_ratio": args.min_island_ratio,
        "faces_before": raw_imported_faces,
        "faces_after_weld": total_faces_before,
        "faces_after_island_removal": faces_after_islands,
        "faces_after": faces_after_decimate,
        "islands_total": len(islands),
        "islands_removed": removed_islands,
        "islands_kept": len(islands) - removed_islands,
        "removed_faces": removed_faces,
        "removed_faces_ratio": round(removed_faces / max(1, total_faces_before), 6),
        "total_area_before_m2": round(total_area_before, 4),
        "total_area_after_m2": round(total_area_after, 4),
        "removed_area_m2": round(removed_area, 4),
        "removed_area_ratio": round(removed_area / max(1e-12, total_area_before), 6),
        "decimate_skipped": decimate_skipped,
        "decimate_ratio": round(decimate_ratio, 6),
        "mesh_objects_after": len(mesh_objs),
        "export_normals": export_normals,
    }

    report_path = (out_dir / args.out_report).resolve()
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    argv = sys.argv
    script_args = argv[argv.index("--") + 1 :] if "--" in argv else argv[1:]
    parsed = parse_args(script_args)
    report = run_blender_cleanup(parsed)
    print(
        f"[blender_cleanup] faces: {report['faces_before']} -> "
        f"{report['faces_after_island_removal']} (islands removed: {report['islands_removed']}/{report['islands_total']}) -> "
        f"{report['faces_after']} (decimate_skipped={report['decimate_skipped']})",
        flush=True,
    )


if __name__ == "__main__":
    main()
