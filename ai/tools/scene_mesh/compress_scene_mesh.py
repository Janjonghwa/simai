"""노트북 배경 메시(textured.glb) 정리·압축 및 Scene CANDIDATE 등록 CLI.

사용 예시:
  uv run --project ai python ai/tools/scene_mesh/compress_scene_mesh.py \\
    --in C:/Users/SSAFY/simai-local/session9/scene-3ce8c02f/textured.glb \\
    --out C:/Users/SSAFY/simai-local/scene-mesh-404 \\
    --target-faces 400000 \\
    --min-island-ratio 0.001
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from simai_ai.scene.mesh_compress import (
    DEFAULT_KTX2_MODE,
    DEFAULT_MIN_ISLAND_RATIO,
    DEFAULT_TARGET_FACES,
    DEFAULT_TOKEN_ENV_VAR,
    compress_scene_mesh,
    register_scene_candidate,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Blender 5.2 헤드리스 조각 제거·UV 보존 Decimate + "
            "@gltf-transform/cli@4.5.1 (meshopt + KTX2) 배경 메시 압축 도구"
        ),
    )
    parser.add_argument(
        "--in", dest="input_glb", required=True, help="입력 textured.glb 경로"
    )
    parser.add_argument(
        "--out", dest="output_dir", required=True, help="출력 디렉터리 경로"
    )
    parser.add_argument(
        "--target-faces",
        type=int,
        default=DEFAULT_TARGET_FACES,
        help=f"Decimate COLLAPSE 목표 면 수 (기본값: {DEFAULT_TARGET_FACES})",
    )
    parser.add_argument(
        "--min-island-ratio",
        type=float,
        default=DEFAULT_MIN_ISLAND_RATIO,
        help=f"제거할 부유 조각의 전체 면/면적 대비 최소 비율 (기본값: {DEFAULT_MIN_ISLAND_RATIO})",
    )
    parser.add_argument(
        "--ktx2-mode",
        choices=("uastc", "etc1s"),
        default=DEFAULT_KTX2_MODE,
        help=f"사진 텍스처 KTX2 압축 방식 (기본값: {DEFAULT_KTX2_MODE})",
    )
    parser.add_argument(
        "--uastc-level",
        type=int,
        default=0,
        choices=(0, 1, 2, 3, 4),
        help="UASTC 품질 레벨 (0=Fastest..4=VerySlow, 기본값: 0)",
    )
    parser.add_argument(
        "--zstd-level",
        type=int,
        default=3,
        help="UASTC Zstandard 초압축 레벨 (1..22, 기본값: 3)",
    )
    parser.add_argument(
        "--etc1s-quality",
        type=int,
        default=128,
        help="ETC1S 품질 레벨 (1..255, 기본값: 128)",
    )
    parser.add_argument(
        "--out-name",
        default="textured.glb",
        help="출력 GLB 파일명 (기본값: textured.glb)",
    )
    parser.add_argument(
        "--report-name",
        default="report.json",
        help="출력 리포트 JSON 파일명 (기본값: report.json)",
    )
    parser.add_argument(
        "--blender",
        default=None,
        help="Blender 5.2 실행 파일 경로 (미지정 시 BLENDER_BIN 환경변수 또는 기본 설치 경로 탐색)",
    )

    # 선택적 Scene CANDIDATE 등록 옵션 (관리자 토큰은 환경변수에서만 읽음)
    parser.add_argument(
        "--register-scene",
        action="store_true",
        help="압축 완료 후 관리자 API로 새 Scene 버전(CANDIDATE)을 등록",
    )
    parser.add_argument(
        "--server-url", default=None, help="서버 기본 URL (예: https://simai.live)"
    )
    parser.add_argument("--site-id", default=None, help="등록할 Site ID")
    parser.add_argument(
        "--aligned-to-calibration-id",
        default=None,
        help="정합 기준 Calibration ID",
    )
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV_VAR,
        help=f"관리자 Bearer 토큰을 읽을 환경변수 이름 (기본값: {DEFAULT_TOKEN_ENV_VAR})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.register_scene and (
        not args.server_url or not args.site_id or not args.aligned_to_calibration_id
    ):
        parser.error(
            "--register-scene 사용 시 --server-url, --site-id, --aligned-to-calibration-id 가 모두 필요합니다."
        )

    report = compress_scene_mesh(
        args.input_glb,
        args.output_dir,
        target_faces=args.target_faces,
        min_island_ratio=args.min_island_ratio,
        ktx2_mode=args.ktx2_mode,
        uastc_level=args.uastc_level,
        zstd_level=args.zstd_level,
        etc1s_quality=args.etc1s_quality,
        output_glb_name=args.out_name,
        report_name=args.report_name,
        blender_bin=args.blender,
    )

    if args.register_scene:
        reg_result = register_scene_candidate(
            server_url=args.server_url,
            site_id=args.site_id,
            aligned_to_calibration_id=args.aligned_to_calibration_id,
            glb_path=report["output_path"],
            report=report,
            token_env_var=args.token_env,
        )
        report["scene_registration"] = {
            "sceneId": reg_result["sceneId"],
            "status": reg_result["status"],
            "gltfAssetKey": reg_result["gltfAssetKey"],
        }

    summary = {
        "output_path": report["output_path"],
        "faces_before": report["faces_before"],
        "faces_after": report["faces_after"],
        "islands_removed": report["islands_removed"],
        "bytes_before": report["bytes_before"],
        "bytes_after": report["bytes_after"],
        "size_reduction_ratio": report["size_reduction_ratio"],
        "extensions_used": report["extensions_used"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
