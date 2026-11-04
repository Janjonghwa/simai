"""Tier 0 배경 재내보내기와 기존 정적 복원 제출을 연결한다(S15P31A101-427, -461)."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from simai_ai.scene.clean_bg_job import (
    DEFAULT_RECENT_WINDOW_FRAMES,
    DEFAULT_SEAM_BLEND_RADIUS_PX,
    refine_bundle_backgrounds,
)

from .auto_static import AutoStaticSubmitter, BUNDLE_RE, CALIBRATION_STATES
from .handlers import JobContext, JobFailed


class SceneRegenerateHandler:
    def __init__(
        self,
        submitter: AutoStaticSubmitter,
        marker: Path,
        *,
        timeout_s: float = 120.0,
        window_frames: int = DEFAULT_RECENT_WINDOW_FRAMES,
        blend_radius_px: int = DEFAULT_SEAM_BLEND_RADIUS_PX,
        previous_bundle_dir: Path | None = None,
        previous_backgrounds: Mapping[str, Any] | None = None,
    ) -> None:
        self.submitter, self.marker, self.timeout_s = submitter, marker, timeout_s
        self.window_frames = window_frames
        self.blend_radius_px = blend_radius_px
        self.previous_bundle_dir = previous_bundle_dir
        self.previous_backgrounds = previous_backgrounds

    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        root = self.submitter.settings.state_dir
        control = root / "clean_background"
        request_id = ctx.input["request_id"]
        if not self.marker.is_file():
            candidate = self.submitter._candidate()
            if isinstance(candidate, str):
                raise JobFailed("TIER0_NOT_RUNNING", "Tier 0 와 완료 묶음이 없다")
            bundle, version = candidate.bundle_dir, candidate.calibration_version
        else:
            control.mkdir(parents=True, exist_ok=True)
            request = control / "regenerate.request.json"
            tmp = control / f".request-{uuid.uuid4().hex}.tmp"
            try:
                tmp.write_text(json.dumps({"request_id": request_id, "requested_at_ms": ctx.input["requested_at_ms"]}), encoding="utf-8")
                tmp.replace(request)
            finally:
                tmp.unlink(missing_ok=True)
            end = time.monotonic() + self.timeout_s
            while True:
                if ctx.cancelled.is_set() or ctx.is_expired() or time.monotonic() >= end:
                    raise JobFailed("TIMEOUT", "배경 응답 대기 시간 초과")
                try:
                    response = json.loads((control / "regenerate.response.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    response = {}
                if response.get("request_id") == request_id:
                    if response.get("status") == "NOT_READY":
                        raise JobFailed("BACKGROUND_NOT_READY", "배경 누적 부족")
                    if response.get("status") == "READY":
                        bundle = Path(response["bundle_path"])
                        version = response["calibration_version"]
                        break
                ctx.cancelled.wait(0.1)
        active = self.submitter._active_calibration()
        if (isinstance(active, str) or active[0] != version or active[2] not in CALIBRATION_STATES
                or not BUNDLE_RE.fullmatch(bundle.name) or not (bundle / "COMPLETE").is_file()):
            raise JobFailed("BACKGROUND_NOT_READY", "완료 묶음 또는 활성 보정이 유효하지 않다")
        try:
            quality = refine_bundle_backgrounds(
                bundle,
                previous_bundle_dir=self.previous_bundle_dir,
                previous_backgrounds=self.previous_backgrounds,
                window_frames=self.window_frames,
                blend_radius_px=self.blend_radius_px,
            )
            job_input = dict(self.submitter.prepare(bundle, active[1], lambda: not ctx.cancelled.is_set()))
            if quality is not None and "regenerate_quality" not in job_input:
                job_input["regenerate_quality"] = quality.to_dict()
            result = self.submitter._post({"bundle_id": bundle.name, "input": job_input, "request_id": request_id})
        except Exception as exc:
            raise JobFailed("UPLOAD_FAILED", type(exc).__name__) from exc
        callback_result: dict[str, Any] = {
            "bundle_id": bundle.name,
            "static_reconstruct_job_id": result["job_id"],
        }
        if quality is not None:
            callback_result["quality"] = quality.to_dict()
        elif isinstance(job_input.get("regenerate_quality"), dict):
            callback_result["quality"] = job_input["regenerate_quality"]
        return callback_result
