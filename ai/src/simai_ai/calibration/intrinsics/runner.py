"""여러 카메라의 내부 파라미터 추정 실행: 메모리 수집 → 시간 중앙값 배경 → 카메라별 추정(백엔드 순서) → 같은 렌즈 공유 → 기록.

`cli` 가 이 함수를 부른다. 시험은 백엔드(`IntrinsicsBackendChain`)·열기 함수를 주입해 카메라·가중치 없이 돈다.

- 수집은 카메라마다 스레드 하나로 동시에 한다(4대가 같은 시간대를 본다). 추정은 차례로 한다.
- 배경 영상은 추정이 끝나면 버린다. 기록에는 파라미터와 수집 요약만 남는다(영상·이미지 없음, 명세 11.5).
- 수집에 실패한 카메라(열기·디코딩 실패, 프레임 부족, 계정 없음)는 추정하지 않고 status FAILED 로 비운다.
  영상 크기를 모르므로 `estimates` 에는 넣지 않고 `cameras` 에 사유를 남긴다.
- 같은 렌즈 공유는 추정이 있는 카메라로만 한다. 영상 크기가 서로 다르면 공유하지 않고 사유를 남긴다.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from simai_ai.calibration.intrinsics.capture import (
    CAPTURE_FAILED,
    CAPTURE_OK,
    SOURCE_FILE,
    SOURCE_RTSP,
    CameraInput,
    CaptureConfig,
    CaptureResult,
    Opener,
    RtspCredentials,
    capture_background,
)
from simai_ai.calibration.intrinsics.chain import ChainResult, IntrinsicsBackendChain
from simai_ai.calibration.intrinsics.estimate import STATUS_FAILED, IntrinsicsEstimate
from simai_ai.calibration.intrinsics.record import build_intrinsics_record
from simai_ai.calibration.intrinsics.shared import SharedLensConfig, SharedLensResult, combine_shared_lens

RUNNER_VERSION = "0.1.0"
# input_provenance.frames.source 값 (common/defs.schema.json frame_source)
FILE_FRAME_SOURCES = ("FILE", "SIM", "DATASET")


@dataclass
class CameraRun:
    camera_id: str
    capture: CaptureResult
    chain: ChainResult | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def estimate(self) -> IntrinsicsEstimate | None:
        return None if self.chain is None else self.chain.estimate

    @property
    def status(self) -> str:
        if self.capture.status != CAPTURE_OK or self.chain is None:
            return STATUS_FAILED
        return self.chain.estimate.status

    def to_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "camera_id": self.camera_id,
            "status": self.status,
            "source": dict(self.capture.source),
            "capture": self.capture.to_record(),
        }
        if self.chain is not None:
            rec["method"] = self.chain.estimate.method
            rec["backend"] = self.chain.to_record()
        reasons = list(self.reasons)
        if self.capture.status != CAPTURE_OK:
            reasons += self.capture.reasons
        elif self.chain is not None and self.chain.estimate.status == STATUS_FAILED:
            reasons += list(self.chain.estimate.uncertainty.notes[-1:])
        if reasons:
            rec["reasons"] = reasons
        return rec


@dataclass
class IntrinsicsRun:
    record: dict[str, Any]
    cameras: list[CameraRun]
    shared: SharedLensResult | None


def frames_source_for(cameras: Sequence[CameraInput], file_source: str = "FILE") -> str:
    """input_provenance.frames.source: RTSP 는 LIVE, 파일은 file_source(FILE·SIM·DATASET). 섞으면 ValueError."""
    kinds = {c.kind for c in cameras}
    if len(kinds) != 1:
        raise ValueError(f"RTSP 와 파일 입력을 한 실행에 섞지 않는다(입력 출처가 하나여야 한다): {sorted(kinds)}")
    if kinds == {SOURCE_RTSP}:
        return "LIVE"
    if file_source not in FILE_FRAME_SOURCES:
        raise ValueError(f"파일 입력의 출처는 {FILE_FRAME_SOURCES} 중 하나다: {file_source!r}")
    assert kinds == {SOURCE_FILE}
    return file_source


def run_intrinsics(
    cameras: Sequence[CameraInput],
    *,
    chain: IntrinsicsBackendChain,
    capture_config: CaptureConfig,
    created_at: int,
    frames_source: str,
    credentials: Mapping[str, RtspCredentials] | None = None,
    credential_errors: Mapping[str, str] | None = None,
    shared_config: SharedLensConfig | None = None,
    opener: Opener | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> IntrinsicsRun:
    """카메라들을 동시에 수집하고 차례로 추정해 기록 한 건을 만든다(파일을 쓰지 않는다)."""
    ids = [c.camera_id for c in cameras]
    if not cameras:
        raise ValueError("카메라가 없다")
    if len(set(ids)) != len(ids):
        raise ValueError(f"같은 camera_id 가 두 번 있다: {ids}")
    creds = dict(credentials or {})
    cred_errors = dict(credential_errors or {})

    def collect(cam: CameraInput) -> CaptureResult:
        if cam.camera_id in cred_errors:
            return CaptureResult(cam.camera_id, cam.describe(), CAPTURE_FAILED, reasons=[cred_errors[cam.camera_id]])
        return capture_background(
            cam, capture_config, credentials=creds.get(cam.camera_id), monotonic=monotonic, opener=opener
        )

    with ThreadPoolExecutor(max_workers=len(cameras)) as pool:
        captures = list(pool.map(collect, cameras))

    runs: list[CameraRun] = []
    for cap in captures:
        run = CameraRun(cap.camera_id, cap)
        if cap.status == CAPTURE_OK and cap.background is not None:
            run.chain = chain.estimate_with_trace(cap.background, camera_id=cap.camera_id)
        cap.background = None  # 배경 영상은 추정 뒤 버린다
        runs.append(run)

    estimates = [r.estimate for r in runs if r.estimate is not None]
    shared: SharedLensResult | None = None
    shared_skipped: str | None = None
    if estimates:
        try:
            shared = combine_shared_lens(estimates, shared_config)
        except ValueError as e:
            shared_skipped = f"같은 렌즈 공유를 하지 않았다: {e}"
    else:
        shared_skipped = "추정한 카메라가 없어 같은 렌즈 공유를 하지 않았다"

    run_section: dict[str, Any] = {
        "runner_version": RUNNER_VERSION,
        "capture": capture_config.to_record(),
        "backend_order": list(chain.order),
        "images_stored": False,
    }
    if chain.unavailable:
        run_section["unavailable_backends"] = dict(chain.unavailable)
    if shared_skipped is not None:
        run_section["shared_lens_skipped"] = shared_skipped
    record = build_intrinsics_record(
        created_at=created_at,
        frames={"source": frames_source, "camera_ids": ids},
        estimates=estimates,
        shared=shared,
        cameras=[r.to_record() for r in runs],
        run=run_section,
    )
    return IntrinsicsRun(record, runs, shared)
