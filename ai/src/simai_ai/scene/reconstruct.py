"""정적 복원 작업 순서와 I5 결과. 도구 실행·저장소·시계는 교체 가능하다."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from simai_ai.providers.contract import schema_errors

SCENE_SCHEMA = "scene_output"
SURFACE_VALUES = {"0": "UNOBSERVED", "1": "INTERPOLATED", "2": "OBSERVED", "3": "ESTIMATED_PLANE", "4": "GENERATED"}
TIMEOUTS = {"download": 120, "prepare": 180, "mapanything": 1200, "preview": 60, "bake": 1800,
            "fallback": 120, "measure": 180, "upload": 180}


class ReconstructionError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message)
        self.code = code


class Stages(Protocol):
    def run(self, stage: str, root: Path, *, timeout_s: float, cancelled: threading.Event) -> None: ...


def sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_input(inp: Mapping[str, Any]) -> None:
    """job_request.input 은 자유 객체다. 티켓의 입력 프로파일만 검사하고 계약은 바꾸지 않는다."""
    try:
        for key in ("calibration_version", "calibration_ref", "calibration_hash", "input_provenance", "images"):
            if key not in inp:
                raise ValueError(f"필수 입력 없음: {key}")
        if not isinstance(inp["calibration_version"], str) or not inp["calibration_version"]:
            raise ValueError("보정 버전이 비었다")
        _uri(inp["calibration_ref"])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", inp["calibration_hash"]):
            raise ValueError("보정 해시 형식 오류")
        images = inp["images"]
        if not isinstance(images, list) or not 2 <= len(images) <= 16:
            raise ValueError("카메라 이미지 2~16장이 필요하다")
        ids = []
        for im in images:
            cid = im["camera_id"]
            if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid):
                raise ValueError("카메라 ID 형식 오류")
            ids.append(cid)
            _uri(im["uri"])
            if im["kind"] not in ("CLEAN_BACKGROUND", "FRAME"):
                raise ValueError("이미지 kind 는 CLEAN_BACKGROUND 또는 FRAME")
        if len(set(ids)) != len(ids):
            raise ValueError("중복 카메라")
        if errors := schema_errors(inp["input_provenance"], {"$ref": "https://simai.dev/contracts/common/defs.schema.json#/$defs/input_provenance"}):
            raise ValueError("; ".join(errors))
        if set(inp["input_provenance"]["frames"].get("camera_ids", [])) != set(ids):
            raise ValueError("출처 camera_ids 와 입력이 다르다")
        opts = inp.get("options", {})
        if not isinstance(opts, Mapping):
            raise ValueError("options 는 객체여야 한다")
        allowed_opts = {
            "align_views",
            "plane_cleanup",
            "mode",
            "preview_only",
            "emit_preview",
            "preview_stride",
            "target_faces",
            "num_threads",
        }
        if set(opts) - allowed_opts:
            raise ValueError("모르는 복원 옵션")
        if opts.get("align_views", "off") not in ("off", "sim3", "scale"):
            raise ValueError("align_views 형식 오류")
        if not isinstance(opts.get("plane_cleanup", False), bool):
            raise ValueError("plane_cleanup 은 bool")
        if opts.get("mode", "full") not in ("full", "preview", "two_stage"):
            raise ValueError("mode 형식 오류")
        if not isinstance(opts.get("preview_only", False), bool):
            raise ValueError("preview_only 는 bool")
        if not isinstance(opts.get("emit_preview", False), bool):
            raise ValueError("emit_preview 는 bool")
        if "preview_stride" in opts:
            ps = opts["preview_stride"]
            if isinstance(ps, bool) or not isinstance(ps, int) or ps < 1:
                raise ValueError("preview_stride 는 1 이상 정수")
        if "target_faces" in opts:
            tf = opts["target_faces"]
            if isinstance(tf, bool) or not isinstance(tf, int) or tf < 100:
                raise ValueError("target_faces 는 100 이상 정수")
        if "num_threads" in opts:
            nt = opts["num_threads"]
            if isinstance(nt, bool) or not isinstance(nt, int) or nt < 0:
                raise ValueError("num_threads 는 0 이상 정수")
    except (TypeError, KeyError, ValueError) as e:
        raise ReconstructionError("STATIC_INPUT_INVALID", str(e)) from e


def _uri(uri: Any) -> None:
    if not isinstance(uri, str) or not re.fullmatch(r"s3://[^/\s]+/[^\s]+", uri):
        raise ValueError("객체 저장소 s3:// 주소가 필요하다")


class StaticReconstructor:
    def __init__(self, stages: Stages, *, clock_ms: Callable[[], int], monotonic: Callable[[], float] = time.monotonic,
                 timeouts: Mapping[str, float] | None = None) -> None:
        self.stages, self.clock_ms, self.monotonic = stages, clock_ms, monotonic
        self.timeouts = {**TIMEOUTS, **(timeouts or {})}

    def run(self, inp: Mapping[str, Any], root: Path, *, run_id: str, cancelled: threading.Event,
            progress: Callable[[float], None], get: Callable[[str], bytes],
            put: Callable[[str, bytes, str], str],
            on_preview: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        validate_input(inp)
        started_at = self.clock_ms()
        metrics: dict[str, Any] = {"stage_seconds": {}, "texture": None, "scale_check": None,
                                   "safety_use": False, "input_provenance": inp["input_provenance"]}

        def check(stage: str, started: float) -> None:
            if cancelled.is_set():
                raise ReconstructionError("CANCELLED", "작업 취소")
            if self.monotonic() - started >= self.timeouts[stage]:
                raise ReconstructionError(f"STATIC_{stage.upper()}_TIMEOUT", "단계 시간 초과")

        def timed(stage: str, action: Callable[[], Any], fraction: float) -> Any:
            started = self.monotonic()
            check(stage, started)
            result = action()
            check(stage, started)
            metrics["stage_seconds"][stage] = self.monotonic() - started
            progress(fraction)
            return result

        def tool(stage: str, fraction: float) -> None:
            timed(stage, lambda: self.stages.run(stage, root, timeout_s=self.timeouts[stage], cancelled=cancelled), fraction)

        def output(path: Path, stage: str) -> Any:
            if not path.is_file():
                raise ReconstructionError(f"STATIC_{stage.upper()}_EMPTY", "단계 결과 문서 없음")
            return read_json(path)

        def download() -> dict[str, Any]:
            started = self.monotonic()
            raw = get(inp["calibration_ref"])
            check("download", started)
            if sha256(raw) != inp["calibration_hash"]:
                raise ReconstructionError("CALIBRATION_HASH_MISMATCH", "보정 바이트 해시 불일치")
            try:
                cal = json.loads(raw)
            except ValueError as e:
                raise ReconstructionError("CALIBRATION_INVALID", "JSON 오류") from e
            if schema_errors(cal, "calibration") or cal["calibration_version"] != inp["calibration_version"]:
                raise ReconstructionError("CALIBRATION_INVALID", "보정 형식·버전 불일치")
            if cal["state"] not in ("PROVISIONAL", "CALIBRATED"):
                raise ReconstructionError("CALIBRATION_INVALID", "World 보정이 유효하지 않음")
            if not {im["camera_id"] for im in inp["images"]} <= {c["camera_id"] for c in cal["cameras"]}:
                raise ReconstructionError("CALIBRATION_INVALID", "카메라 보정 없음")
            provenance = inp["input_provenance"].get("calibration", {})
            if provenance != {"source": cal["provider_source"], "calibration_version": cal["calibration_version"]}:
                raise ReconstructionError("STATIC_INPUT_INVALID", "보정 출처 불일치")
            if (cal["provider_source"] in ("SIM", "DATASET_GT") or inp["input_provenance"]["frames"]["source"] in ("SIM", "DATASET_GT")) and not inp["input_provenance"]["is_upper_bound"]:
                raise ReconstructionError("STATIC_INPUT_INVALID", "정답·SIM 은 상한 표시 필수")
            (root / "images").mkdir()
            write_json(root / "calibration.json", cal)
            hashes = []
            for i, im in enumerate(inp["images"]):
                check("download", started)
                image = get(im["uri"])
                check("download", started)
                hashes.append(sha256(image))
                (root / "images" / f"{im['camera_id']}.image").write_bytes(image)
                progress(.15 * (i + 1) / len(inp["images"]))
            write_json(root / "job.json", {"input": inp, "run_id": run_id, "image_hashes": hashes})
            return cal

        progress(0)
        cal = timed("download", download, .15)
        tool("prepare", .25)
        tool("mapanything", .55)
        model = output(root / "mapanything" / "metrics.json", "mapanything")
        if model.get("result") != "SUCCESS":
            raise ReconstructionError("STATIC_MAPANYTHING_EMPTY", "MapAnything 결과 없음")
        opts = inp.get("options", {})
        mode = "preview" if opts.get("preview_only") else opts.get("mode", "full")
        want_two_stage = (mode == "two_stage") or bool(opts.get("emit_preview")) or (on_preview is not None)
        image_hashes = read_json(root / "job.json")["image_hashes"]
        input_hash = sha256(json.dumps({"input": inp, "image_hashes": image_hashes}, sort_keys=True).encode())
        preview_emitted: dict[str, Any] | None = None
        if mode == "preview":
            tool("preview", .75)
            bake = output(root / "preview.json", "preview")
        else:
            if want_two_stage:
                tool("preview", .62)
                prev_doc = output(root / "preview.json", "preview")
                prev_assets = []
                for pspec in prev_doc["assets"]:
                    ppath = (root / pspec["path"]).resolve()
                    if not ppath.is_relative_to(root.resolve()) or not ppath.is_file() or ppath.stat().st_size == 0:
                        raise ReconstructionError("STATIC_OUTPUT_EMPTY", f"미리보기 산출물 경로·내용 오류: {pspec['path']}")
                    pcontent = ppath.read_bytes()
                    passet: dict[str, Any] = {
                        "kind": pspec["kind"],
                        "uri": put(ppath.name, pcontent, pspec["content_type"]),
                        "sha256": sha256(pcontent),
                        "bytes": len(pcontent),
                        "axis_convention": "GLTF_Y_UP",
                    }
                    if pspec.get("triangle_count") is not None:
                        passet["triangle_count"] = int(pspec["triangle_count"])
                    prev_assets.append(passet)
                preview_emitted = {
                    "schema_version": "2.1.0",
                    "scene_version": f"scene-preview-{run_id}",
                    "state": "CANDIDATE",
                    "geometry_role": "VISUAL",
                    "created_at": self.clock_ms(),
                    "calibration_version": inp["calibration_version"],
                    "coordinate_frame": cal["coordinate_frame"],
                    "input_provenance": inp["input_provenance"],
                    "reproducibility": {
                        "run_id": run_id,
                        "model_version": model["model_version"],
                        "input_hash": input_hash,
                        **({"weights_hash": model["weights_hash"]} if model.get("weights_hash") else {}),
                    },
                    "method": {
                        "reconstructor": "MAPANYTHING_NATIVE",
                        "reconstructor_detail": "preview_scene",
                        "uses_known_pose": False,
                        "human_removal": "MASK_COMPOSITE",
                        "postprocess": prev_doc["postprocess"],
                    },
                    "assets": prev_assets,
                    "quality": {
                        "wall_deviation_m": None,
                        "observed_surface_ratio": prev_doc["observed_surface_ratio"],
                        "notes": "벽 GT 미측정. scene_tier=PREVIEW; preview_scene=true; texture=null: PREVIEW_FAST_PATH",
                    },
                }
                if perrors := schema_errors(preview_emitted, SCENE_SCHEMA):
                    raise ReconstructionError("STATIC_OUTPUT_INVALID", "; ".join(perrors))
                write_json(root / "preview_scene_output.json", preview_emitted)
                prev_out_uri = put("preview_scene_output.json", (root / "preview_scene_output.json").read_bytes(), "application/json")
                metrics["preview_scene"] = {
                    "scene_version": preview_emitted["scene_version"],
                    "glb_uri": prev_assets[0]["uri"],
                    "output_uri": prev_out_uri,
                    "metrics": prev_doc.get("metrics"),
                }
                if on_preview is not None:
                    on_preview(preview_emitted)
            tool("bake", .8)  # 도구 부재 시 bake 가 fallback 을 선언한다
            bake = output(root / "bake.json", "bake")
        if mode != "preview" and bake.get("fallback_reason"):
            tool("fallback", .85)
            bake = output(root / "bake.json", "fallback")
        tool("measure", .9)
        metrics.update(texture=bake.get("texture"), texture_fallback_reason=bake.get("fallback_reason"),
                       scale_check=output(root / "scale_check.json", "measure"), reconstruction=model,
                       bake=bake.get("metrics"), image_hashes=read_json(root / "job.json")["image_hashes"])
        result = {
            "schema_version": "2.1.0", "scene_version": f"scene-preview-{run_id}" if mode == "preview" else f"scene-{run_id}", "state": "CANDIDATE",
            "geometry_role": "VISUAL", "created_at": self.clock_ms(), "calibration_version": inp["calibration_version"],
            "coordinate_frame": cal["coordinate_frame"], "input_provenance": inp["input_provenance"],
            "reproducibility": {"run_id": run_id, "model_version": model["model_version"],
                "input_hash": sha256(json.dumps({"input": inp, "image_hashes": metrics["image_hashes"]}, sort_keys=True).encode())},
            "method": {"reconstructor": "MAPANYTHING_NATIVE", "uses_known_pose": False,
                       **({"reconstructor_detail": "preview_scene"} if mode == "preview" else {}),
                       "human_removal": "MASK_COMPOSITE", "postprocess": bake["postprocess"]},
            "assets": [], "quality": {"wall_deviation_m": None, "observed_surface_ratio": bake["observed_surface_ratio"],
                "notes": "벽 GT 미측정. 출처·축척 점검·지표: metrics.json"
                + ("; scene_tier=PREVIEW; preview_scene=true" if mode == "preview" else (f"; scene_tier=FULL; replaces_preview_scene={preview_emitted['scene_version']}" if preview_emitted is not None else ""))
                + "; texture=" + ("null: " + bake["fallback_reason"] if bake.get("fallback_reason") else "texrecon")},
        }
        if model.get("weights_hash"):
            result["reproducibility"]["weights_hash"] = model["weights_hash"]

        def upload() -> None:
            started = self.monotonic()
            for spec in bake["assets"]:
                check("upload", started)
                path = (root / spec["path"]).resolve()
                if not path.is_relative_to(root.resolve()) or not path.is_file() or path.stat().st_size == 0:
                    raise ReconstructionError("STATIC_OUTPUT_EMPTY", f"산출물 경로·내용 오류: {spec['path']}")
                content = path.read_bytes()
                asset = {"kind": spec["kind"], "uri": put(path.name, content, spec["content_type"]),
                         "sha256": sha256(content), "bytes": len(content)}
                if spec["kind"] == "GLTF_VISUAL":
                    asset["axis_convention"] = "GLTF_Y_UP"
                    if spec.get("triangle_count") is not None:
                        asset["triangle_count"] = int(spec["triangle_count"])
                elif spec["kind"] == "SURFACE_SOURCE_MAP":
                    asset["surface_values"] = SURFACE_VALUES
                    if spec.get("ratios"):
                        asset["ratios"] = spec["ratios"]
                result["assets"].append(asset)
            check("upload", started)
            metrics["auxiliary_assets"] = []
            for rel in bake.get("auxiliary", []):
                check("upload", started)
                path = (root / rel).resolve()
                if not path.is_relative_to(root.resolve()) or not path.is_file() or path.stat().st_size == 0:
                    raise ReconstructionError("STATIC_OUTPUT_EMPTY", f"보조 지도 경로·내용 오류: {rel}")
                content = path.read_bytes()
                metrics["auxiliary_assets"].append({"name": path.name, "uri": put(path.name, content, "image/png"), "sha256": sha256(content)})
            write_json(root / "metrics.json", metrics)
            uri = put("metrics.json", (root / "metrics.json").read_bytes(), "application/json")
            result["quality"]["notes"] += f"; metrics_uri={uri}"
            from .texture_mesh import git_info
            gpu = model.get("gpu", {})
            record = {
                "schema_version": "2.2.0", "run_id": run_id, "experiment_id": "EXP-01", "jira_key": "S15P31A101-364",
                "owner": "static_reconstruct Owner", **git_info(), "env_hash": os.environ.get("SIMAI_IMAGE_DIGEST", "unknown"),
                "model_version": model["model_version"], "config": {"path": "ai/src/simai_ai/scene/reconstruct.py",
                    "sha256": sha256(Path(__file__).read_bytes()), "overrides": inp.get("options", {})},
                "input_provenance": inp["input_provenance"], "started_at": started_at, "finished_at": self.clock_ms(),
                "status": "SUCCEEDED", "experiment_state": "ITERATE",
                "gpu": {"model": str(gpu.get("model", "unknown")), "driver": str(gpu.get("driver", "unknown")),
                    "cuda": str(gpu.get("cuda", "unknown")), "provider": os.environ.get("SIMAI_GPU_PROVIDER", "OTHER")},
                "metrics": {"texture_available": bake.get("texture") is not None, "observed_surface_ratio": bake["observed_surface_ratio"],
                    "scale_check": metrics["scale_check"].get("scale"), **{f"{k}_s": v for k, v in metrics["stage_seconds"].items()}},
                "results": [{"kind": "REPORT", "uri": uri, "sha256": sha256((root / "metrics.json").read_bytes())}],
                "notes": "SIM/DATASET_GT 지표는 제품 성능이 아님. CANDIDATE만 생성; 입력 보정 변경 없음.",
            }
            if errors := schema_errors(record, "run_record"):
                raise ReconstructionError("STATIC_OUTPUT_INVALID", "; ".join(errors))
            write_json(root / "run_record.json", record)
            check("upload", started)
            run_uri = put("run_record.json", (root / "run_record.json").read_bytes(), "application/json")
            result["quality"]["notes"] += f"; run_record_uri={run_uri}"
            for log in sorted(root.glob("*.log")):
                check("upload", started)
                if log.stat().st_size == 0:
                    continue
                log_uri = put(log.name, log.read_bytes(), "text/plain")
                result["quality"]["notes"] += f"; {log.name}={log_uri}"

        timed("upload", upload, .99)
        if errors := schema_errors(result, SCENE_SCHEMA):
            raise ReconstructionError("STATIC_OUTPUT_INVALID", "; ".join(errors))
        if mode == "preview" and on_preview is not None:
            on_preview(result)
        progress(1)
        return result
