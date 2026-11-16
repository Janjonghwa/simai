"""`site_setup` 운영 단계 어댑터(S15P31A101-465): 같은 Pod 의 Tier 0 배경 묶음·MapAnything·미리보기·굽기를 `SiteSetupHandler` 주입점에 잇는다.

한 작업(대여)마다 임시 작업 폴더 하나(`root`)를 쓰고, 기존 정적 3D 단계 도구(`simai_ai.scene.reconstruct_tasks` 의
prepare·mapanything·preview·bake·fallback)를 `Stages`(기본 `ProcessStages`, 취소·시간 제한 지킴)로 돌린다. 그 폴더 레이아웃은
`static_reconstruct` 와 같다(images/·calibration.json·job.json). 그래서 -451 미리보기 맵·굽기 코드를 그대로 쓴다.

- (a) `collect`: 세팅을 시작한 시각(첫 호출 때 처리기 시계)을 표본 하한으로 Tier 0 에 배경 촬영을 요청하고
  (`SIMAI_STATE_DIR/clean_background/regenerate.request.json` 의 `fresh_since_ms`, S15P31A101-686), 그 뒤 표본만으로 만든 묶음이
  READY 가 될 때까지 응답의 카메라별 상태(표본 수·사람 가림·영상 없음)를 수집률과 `background_capture` 로 돌려준다. 옛 완성 묶음·
  세팅 기준은 쓰지 않는다(15차: 카메라를 돌린 뒤에도 회전 중 화면 묶음을 써서 FLOOR_NOT_FOUND). 처리기가
  `SIMAI_SETUP_BACKGROUND_WAIT_S`(기본 240 s)까지 다시 보고, 그래도 안 되면 재시도 가능한 `BACKGROUND_INCOMPLETE`. 작업이 끝나면
  (성공·실패·취소) 남은 요청을 거둔다. 묶음의 마지막 관측 시각이 보정 문서 created_at 이 된다(S15P31A101-473).
- (b) 내부 파라미터: Tier 0 활성 보정 문서의 K·왜곡을 재사용한다. 없으면 `INTRINSICS_UNAVAILABLE` 로 끝낸다(무마커 추정 경로는 이 처리기에 아직
  연결하지 않았다, 지어내지 않는다).
- (c)(d) `initialize`: prepare → mapanything 단계 → `build_world_from_pointcloud_and_poses`(바닥 Z=0, 벽 +X, 모서리 원점, 카메라 높이 축척).
- (f) `anchor_world`: Stage A 뒤 새 보정을 Tier 0 활성 보정(이전 채택 World)과 카메라 중심으로 비교해, 카메라가 움직이지
  않았으면 이전 World 로 옮긴다(yaw + 수평 이동, 바닥·축척 유지). 아니면 새 World 그대로 두고 사유를 `world_anchor` 에 남긴다
  (S15P31A101-584, docs/design/world-reanchor.md).
- (f) `publish`: 새 보정 문서를 작업 폴더에 두고 객체 저장소에 올린다. 올린 객체 참조(주소·sha256·크기)를 함께 돌려주면 처리기가
  `artifacts.calibration_document` 로 알리고, EC2 가 그 문서를 읽어 새 calibration_version 으로 등록한다(S15P31A101-467).
- (g) `preview`: reconstruct_tasks `preview` 단계 → GLB 업로드 → `scene-preview-<run>`(+ `artifacts.preview_scene`).
- (g) `preview` 는 그 단계가 쓴 바닥 감시 범위(`floor_coverage.json`, S15P31A101-606)가 있고 계약에 맞으면 함께 돌려준다.
- (g) `publish_frames`: 사람 지운 배경에서 카메라별 대표 프레임 1장(JPEG, 긴 변 1280 px 이하)을 같은 산출물 경로로 올리고 카메라 ID·sha256 과
  함께 `artifacts.camera_frames` 로 알린다. EC2 가 이를 AI 구역 초안(`vlm_zone_draft`) 입력 `frames` 에 싣는다(S15P31A101-479).
- (g) `publish_bake_images`: 미리보기·굽기가 읽는 작업 폴더 `images/<카메라>.image`(배경 묶음 사진 원본) 를 카메라당 1장
  `bake-<카메라>.jpg` 로 올리고 묶음 ID 와 함께 `artifacts.bake_images` 로 알린다. JPEG 이고 8 MiB 이하면 바이트 그대로, 아니면 같은
  해상도 JPEG(품질 90)로 바꾼다. EC2 가 이 대여의 Scene 에 묶어 관제 카메라 시점 VDT 사진으로 서명해 준다(S15P31A101-593).
- (h) `bake`: reconstruct_tasks `bake`(도구 부재 시 `fallback`) → GLB 업로드 → `scene-<run>`(+ `artifacts.scene`).
  입력에 `supplement_media` 가 있으면 굽기 전에 보조 촬영 파일을 서명 GET 으로 받아 `supplement` 단계(묶음별 MapAnything·고정
  카메라 정렬·관문·병합, S15P31A101-605)를 돌린다. 적용되면 병합본으로 굽고, 그 굽기가 실패하면 보조 촬영 없이 다시 굽는다
  (요약 FALLBACK·BAKE_FAILED). 어느 경우에도 세팅은 보조 촬영 때문에 실패하지 않는다.
- `rebake`(mode SUPPLEMENT_REBAKE): `rebake_base` 의 보정 문서·굽기 입력 사진만 받아 같은 작업 폴더를 만들고 prepare →
  mapanything(고정 카메라) → supplement → (적용될 때만) bake → GLB 업로드. 적용되지 않으면 굽지 않고 기준 Scene 을 그대로 돌려준다.

단계가 실패하면 그 단계 로그(`<단계>.log`)의 끝 줄을 워커 로그와 실패 메시지에 남기고, 작업 폴더를 지우기 전에 로그·오류 문서를
`SIMAI_STATE_DIR/worker/site-setup-failed/<작업>-<대여>/` 에 복사한다(S15P31A101-473).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.calibration.extrinsics.feature_matching import match_sift
from simai_ai.scene import supplement_capture as sc
from simai_ai.scene.reconstruct import (
    ALIGNMENT_REFERENCE_FILE,
    ReconstructionError,
    Stages,
    read_json,
    sha256,
    write_json,
)
from simai_ai.scene.zone_height_diff import (
    REASON_NO_PREVIOUS_SCENE,
    REASON_SCENE_UNREADABLE,
    REASON_WORLD_NOT_ANCHORED,
    build_unknown_zone_height_changes,
    evaluate_zone_height_changes,
)

from .auto_static import AutoStaticSettings, calibration_document_path
from .camera_check import CameraCheckRunner
from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError
from .site_setup import (
    MAX_BAKE_IMAGES,
    MAX_CAMERA_FRAMES,
    BakedScene,
    BakeImageRef,
    BakeImages,
    BakeReclaimed,
    CameraFrameRef,
    CameraPose,
    CleanBackgroundBundle,
    InitialWorldReconstruction,
    ObjectRef,
    PreviewDraftResult,
    PublishedCalibration,
    RebakeOutcome,
    SiteSetupHandler,
    build_world_from_pointcloud_and_poses,
    floor_coverage_problems,
)

PREFLIGHT_BUCKET = "preflight"
STAGE_TIMEOUTS_S = {"prepare": 180.0, "mapanything": 1200.0, "preview": 60.0, "bake": 1800.0, "fallback": 120.0,
                    "supplement": 1200.0}
# 보조 촬영을 시작하려면 남아 있어야 하는 작업 시간(보조 단계 + 굽기). 모르면(상한 없음) 시작한다
SUPPLEMENT_MIN_REMAINING_S = 900.0
SUPPLEMENT_MAX_MEDIA_BYTES = 4 << 30  # 계약 size_bytes 상한(4 GiB)
# 보조 촬영을 넣을 수 없는 굽기 경로: 방식 2 는 보정 자세로 WorldMirror 를 조건 거는데 보조 프레임에는 보정 자세가 없다
SUPPLEMENT_UNSUPPORTED_PATHS = ("method2", "method2_calib_wm2", "auto")
# 세팅 배경 촬영은 카메라마다 최소 12장 × 1 s ≈ 12 s(services/tier0/settings.py CleanBackgroundSettings.setup_*, -686).
# 기다림 기본 240 s = 사람이 화면 밖으로 나가 그 카메라만 다시 찍을 여유. 실행 상한(서버 1,800 s)의 배경 촬영 몫
# (site_setup.SETUP_BUDGET_BACKGROUND_S, -688)이라 복원·높이 확인·카메라 확인·굽기 몫을 깎지 않는다
DEFAULT_BACKGROUND_WAIT_S = 240.0
DEFAULT_BACKGROUND_POLL_S = 2.0
# Tier 0 가 아직 응답하지 않았을 때 표시에 쓰는 카메라당 최소 표본 수(Tier 0 기본값). 응답이 오면 그 값을 쓴다
SETUP_CAPTURE_DEFAULT_REQUIRED = 12
# 하한 뒤 이만큼 지나도 Tier 0 가 이 카메라 프레임을 하나도 못 받았으면(또는 응답이 없으면) 영상 없음으로 보인다
CAPTURE_NO_VIDEO_GRACE_MS = 5_000
CAPTURE_REQUEST = "regenerate.request.json"
CAPTURE_RESPONSE = "regenerate.response.json"
CAPTURE_STATES = ("READY", "INSUFFICIENT_SAMPLES", "PERSON_OCCLUDED", "NO_VIDEO")  # 계약 site_setup 1.16.0
FRAME_MAX_LONG_SIDE_PX = 1280  # 구역 초안용 프레임 긴 변 상한(VLM 크롭에 충분, 업로드는 작게)
FRAME_JPEG_QUALITY = 90
# 굽기 입력 사진(VDT) 상한. Tier 0 배경은 이미 JPEG(카메라 해상도, 보통 1 MB 안팎)라 그대로 올린다. 서버 MAX_BAKE_IMAGE_BYTES 와 같다
BAKE_IMAGE_MAX_BYTES = 8 << 20
BAKE_IMAGE_JPEG_QUALITY = 90  # JPEG 가 아니거나 상한을 넘을 때만 같은 해상도로 다시 인코딩(해상도를 줄이지 않아 투영 정렬 불변)
WORLD_ANCHOR_MODES = ("auto", "off")  # SIMAI_SETUP_WORLD_ANCHOR: auto = 카메라가 그대로면 이전 World 로 맞춤(-584), off = 새 World 그대로
LOG_TAIL_LINES = 20  # 실패 단계 로그를 워커 로그에 남길 줄 수
_MESSAGE_TAIL_CHARS = 240  # 실패 메시지(계약 500자)에 붙일 로그 끝 길이
_KEEP_LOG_MAX_BYTES = 8 * 1024 * 1024
FAILED_LOGS_DIR = "site-setup-failed"

log = logging.getLogger("services.worker")


def load_active_calibration(settings: AutoStaticSettings) -> dict[str, Any] | None:
    """Tier 0 보정 버전 저장소의 활성 보정 문서. 읽지 못하면 None(모름)."""
    cal_dir = settings.calibrations_dir
    try:
        head = json.loads((cal_dir / "active.json").read_text(encoding="utf-8"))
        version = head["active"]
        doc = json.loads(calibration_document_path(cal_dir, version).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return doc if isinstance(doc, dict) and doc.get("calibration_version") == version else None


def background_wait_from_env(env: Mapping[str, str]) -> tuple[float, float]:
    """SIMAI_SETUP_BACKGROUND_WAIT_S(0 이상, 기본 240)·SIMAI_SETUP_BACKGROUND_POLL_S(양수, 기본 2). 형식 오류는 ValueError."""
    out = []
    for key, default, allow_zero in (("SIMAI_SETUP_BACKGROUND_WAIT_S", DEFAULT_BACKGROUND_WAIT_S, True),
                                     ("SIMAI_SETUP_BACKGROUND_POLL_S", DEFAULT_BACKGROUND_POLL_S, False)):
        raw = (env.get(key) or "").strip()
        try:
            value = float(raw) if raw else default
        except ValueError:
            raise ValueError(f"{key} 는 초(숫자)다") from None
        if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
            raise ValueError(f"{key} 는 {'0 이상' if allow_zero else '양수'}(초)다")
        out.append(value)
    return out[0], out[1]


def world_anchor_enabled_from_env(env: Mapping[str, str]) -> bool:
    """SIMAI_SETUP_WORLD_ANCHOR(기본 auto). off 면 World 다시 맞추기를 끈다(시연 현장 안전장치). 그 밖의 값은 ValueError."""
    raw = (env.get("SIMAI_SETUP_WORLD_ANCHOR") or "auto").strip().lower()
    if raw not in WORLD_ANCHOR_MODES:
        raise ValueError(f"SIMAI_SETUP_WORLD_ANCHOR 는 {'·'.join(WORLD_ANCHOR_MODES)} 중 하나다")
    return raw == "auto"


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _observed_until_ms(path: Path, meta: Mapping[str, Any]) -> int | None:
    """묶음의 마지막 관측 시각(카메라별 time_range_ms 끝의 최댓값). 없으면 COMPLETE 파일 수정 시각(같은 묶음이면 같다)."""
    ends = []
    cameras = meta.get("cameras")
    if isinstance(cameras, Mapping):
        for entry in cameras.values():
            tr = entry.get("time_range_ms") if isinstance(entry, Mapping) else None
            if isinstance(tr, list) and len(tr) == 2 and isinstance(tr[1], int) and not isinstance(tr[1], bool) and tr[1] > 0:
                ends.append(tr[1])
    if ends:
        return max(ends)
    try:
        return int((path / "COMPLETE").stat().st_mtime * 1000)
    except OSError:
        return None


def _log_tail(path: Path, lines: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()][-lines:]


def _frame_jpeg(source: Any) -> bytes | None:
    """배경 이미지(경로·바이트·BGR 배열) → 긴 변 FRAME_MAX_LONG_SIDE_PX 이하 JPEG 바이트. 읽지·디코드하지 못하면 None."""
    import cv2

    try:
        if isinstance(source, np.ndarray):
            image = source
        else:
            raw = bytes(source) if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
            image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    except (OSError, TypeError, ValueError):
        return None
    if image is None or image.ndim < 2 or image.shape[0] < 1 or image.shape[1] < 1:
        return None
    height, width = image.shape[:2]
    long_side = max(height, width)
    if long_side > FRAME_MAX_LONG_SIDE_PX:
        scale = FRAME_MAX_LONG_SIDE_PX / long_side
        image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), FRAME_JPEG_QUALITY])
    return bytes(encoded.tobytes()) if ok else None


def _bake_image_jpeg(raw: bytes) -> bytes | None:
    """굽기 입력 사진 원본 → 올릴 JPEG. JPEG 이고 상한 안이면 바이트 그대로(같은 파일), 아니면 같은 해상도 JPEG. 못 만들면 None."""
    if raw[:3] == b"\xff\xd8\xff" and len(raw) <= BAKE_IMAGE_MAX_BYTES:
        return raw
    import cv2

    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.ndim < 2 or image.shape[0] < 1 or image.shape[1] < 1:
        return None
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), BAKE_IMAGE_JPEG_QUALITY])
    out = bytes(encoded.tobytes()) if ok else b""
    return out if 0 < len(out) <= BAKE_IMAGE_MAX_BYTES else None


class ProductionSiteSetupStages:
    """대여 하나의 단계 어댑터 묶음. 같은 작업 폴더·객체 저장소·취소 신호를 단계들이 공유한다."""

    def __init__(
        self,
        ctx: JobContext,
        store: ObjectStore,
        settings: AutoStaticSettings,
        stages: Stages,
        root: Path,
        *,
        clock_ms: Callable[[], int],
        calibration_loader: Callable[[AutoStaticSettings], dict[str, Any] | None] = load_active_calibration,
        world_anchor_enabled: bool = True,
    ) -> None:
        self.ctx, self.store, self.settings, self.stages, self.root = ctx, store, settings, stages, root
        self._world_anchor_enabled = world_anchor_enabled
        self._clock_ms = clock_ms
        self._load_calibration = calibration_loader
        self._prior: dict[str, Any] | None = None
        self._bundle_dir: Path | None = None
        self._run_id = f"{ctx.job_id}-{ctx.lease_id}"
        self._baked: dict[str, Any] = {}
        self._rebaking = False  # SUPPLEMENT_REBAKE 는 모든 GPU 단계를 작업 시작(LIVE)으로 회수한다(-621)
        self._prev_glb_loaded = False
        self._prev_glb_bytes: bytes | None = None
        self._capture: dict[str, int] | None = None  # 이 대여의 배경 촬영 요청(request_id·since_ms, -686)

    # ------------------------------------------------------------------ (a)
    def collect(self, *, setup_session_id: str, site_id: str, camera_ids: Sequence[str]) -> CleanBackgroundBundle:
        """세팅 시작 뒤 표본만으로 만든 배경 묶음을 Tier 0 에 요청하고 상태를 돌려준다(-686). 처리기가 다 될 때까지 다시 부른다.

        첫 호출 때 처리기 시계를 표본 하한(since_ms)으로 요청을 쓰고, 그 뒤로는 응답만 읽는다. 다 되기 전에는 묶음 ID 가 빈
        `CleanBackgroundBundle`(수집률 최대 99)과 카메라별 상태 `capture` 를 돌려준다. Tier 0 가 요청을 닫았거나(시간 초과) READY
        묶음이 이 요청·활성 보정과 맞지 않으면 재시도 가능한 BACKGROUND_INCOMPLETE 로 끝낸다(옛 묶음으로 대신하지 않는다).
        """
        now = int(self._clock_ms())
        if self._capture is None:
            self._capture = {"request_id": max(1, now), "since_ms": max(0, now)}
            self._write_capture_request(setup_session_id)
        request_id, since = self._capture["request_id"], self._capture["since_ms"]
        response = self._read_capture_response()
        mine = response if response is not None and response.get("request_id") == request_id else None
        if mine is not None and mine.get("status") == "NOT_READY":
            raise JobFailed("BACKGROUND_INCOMPLETE",
                            f"Tier 0 가 배경 촬영 요청을 닫았다({mine.get('reason')})", retryable=True)
        required = _int_or_none((mine or {}).get("required_samples")) or SETUP_CAPTURE_DEFAULT_REQUIRED
        reported = (mine or {}).get("cameras")
        reported = reported if isinstance(reported, Mapping) else {}
        cameras: dict[str, dict[str, Any]] = {}
        pct: dict[str, float] = {}
        for cid in camera_ids:
            entry = reported.get(cid)
            entry = entry if isinstance(entry, Mapping) else {}
            count = max(0, _int_or_none(entry.get("sample_count")) or 0)
            state = entry.get("state") if entry.get("state") in CAPTURE_STATES else "INSUFFICIENT_SAMPLES"
            last_frame = _int_or_none(entry.get("last_frame_ms"))
            if (count == 0 and state != "NO_VIDEO" and (last_frame is None or last_frame < since)
                    and now - since >= CAPTURE_NO_VIDEO_GRACE_MS):
                state = "NO_VIDEO"  # Tier 0 응답이 없거나 멈춘 경우도 처리기 시계로 알린다
            person = entry.get("person_visible")
            cameras[cid] = {"state": state, "sample_count": count,
                            "person_visible": person if isinstance(person, bool) else None}
            pct[cid] = 99.0 if state == "READY" else round(min(99.0, 100.0 * count / required), 2)
        capture: dict[str, Any] = {"since_ms": since, "required_samples": required, "cameras": cameras}
        interval = _int_or_none((mine or {}).get("sample_interval_ms"))
        if interval is not None and interval > 0:
            capture["sample_interval_ms"] = interval
        if mine is None or mine.get("status") != "READY":
            return CleanBackgroundBundle("", pct, capture=capture)
        return self._ready_bundle(mine, camera_ids, capture)

    def _ready_bundle(self, response: Mapping[str, Any], camera_ids: Sequence[str],
                      capture: Mapping[str, Any]) -> CleanBackgroundBundle:
        assert self._capture is not None
        since = self._capture["since_ms"]
        path = Path(str(response.get("bundle_path") or ""))
        version = response.get("calibration_version")
        try:
            meta = read_json(path / "clean_backgrounds.json")
            cameras = meta["cameras"]
            complete = (path / "COMPLETE").is_file()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise JobFailed("BACKGROUND_INCOMPLETE", f"배경 묶음을 읽지 못했다: {type(exc).__name__}", retryable=True) from exc
        if not complete or meta.get("sample_since_ms") != since or meta.get("calibration_version") != version:
            raise JobFailed("BACKGROUND_INCOMPLETE", "배경 묶음이 이 세팅의 촬영 요청과 맞지 않는다", retryable=True)
        prior = self._load_calibration(self.settings)
        if prior is not None and prior.get("calibration_version") != version:
            # 묶음과 활성 보정이 다르면 그 묶음의 내부 파라미터·해상도를 믿을 수 없다
            raise JobFailed("BACKGROUND_INCOMPLETE", "배경 묶음과 활성 보정 버전이 다르다", retryable=True)
        pct: dict[str, float] = {}
        frames: dict[str, list[Path]] = {}
        for cid in camera_ids:
            entry = cameras.get(cid) if isinstance(cameras, dict) else None
            image = None if not isinstance(entry, dict) else entry.get("image")
            ok = isinstance(entry, dict) and entry.get("ready") is True and isinstance(image, str) and (path / image).is_file()
            if not ok:
                raise JobFailed("BACKGROUND_INCOMPLETE", f"배경 묶음에 쓸 수 있는 {cid} 배경이 없다", retryable=True)
            pct[cid] = 100.0
            frames[cid] = [path / image]
        self._bundle_dir = path
        self._prior = prior
        log.info("작업 %s 세팅 배경 촬영 완료: 묶음 %s (표본 하한 %s)", self.ctx.job_id, path.name, since)
        return CleanBackgroundBundle(path.name, pct, frames, observed_until_ms=_observed_until_ms(path, meta),
                                     capture=capture)

    def _write_capture_request(self, setup_session_id: str) -> None:
        assert self._capture is not None
        control = self.settings.state_dir / "clean_background"
        doc = {
            "request_id": self._capture["request_id"],
            "requested_at_ms": self._capture["since_ms"],
            "fresh_since_ms": self._capture["since_ms"],
            "purpose": "SITE_SETUP",
            "setup_session_id": setup_session_id,
        }
        try:
            control.mkdir(parents=True, exist_ok=True)
            tmp = control / f".request-{uuid.uuid4().hex}.tmp"
            try:
                tmp.write_text(json.dumps(doc), encoding="utf-8")
                tmp.replace(control / CAPTURE_REQUEST)
            finally:
                tmp.unlink(missing_ok=True)
        except OSError as exc:
            raise JobFailed("SETUP_IO_FAILED", f"배경 촬영 요청을 쓰지 못했다: {type(exc).__name__}", retryable=True) from exc
        log.info("작업 %s 세팅 배경 촬영 요청: request_id=%s 표본 하한=%s", self.ctx.job_id, doc["request_id"], doc["fresh_since_ms"])

    def _read_capture_response(self) -> dict[str, Any] | None:
        try:
            doc = json.loads((self.settings.state_dir / "clean_background" / CAPTURE_RESPONSE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def release_capture(self) -> None:
        """이 대여가 쓴 배경 촬영 요청이 아직 남아 있으면 거둔다(Tier 0 는 요청이 사라지면 평소 표본 간격으로 돌아간다)."""
        if self._capture is None:
            return
        request = self.settings.state_dir / "clean_background" / CAPTURE_REQUEST
        try:
            doc = json.loads(request.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(doc, dict) and doc.get("request_id") == self._capture["request_id"]:
            try:
                request.unlink(missing_ok=True)
            except OSError:
                log.warning("작업 %s: 배경 촬영 요청을 거두지 못했다", self.ctx.job_id)

    # ------------------------------------------------------------------ (b)~(d)
    def initialize(
        self,
        *,
        setup_session_id: str,
        site_id: str,
        camera_ids: Sequence[str],
        camera_heights_m: Mapping[str, float],
        height_tolerance_cm: float,
        backgrounds: CleanBackgroundBundle,
        created_at_ms: int,
        progress: Callable[[float], None] | None = None,
    ) -> InitialWorldReconstruction:
        from simai_ai.calibration.world_frame.inputs import (
            load_mapanything_run,
            load_prepared,
        )
        from simai_ai.scene import clean_bg_job as cbj

        prior = self._prior
        if prior is None:
            raise JobFailed(
                "INTRINSICS_UNAVAILABLE",
                "Tier 0 활성 보정 문서가 없어 내부 파라미터를 재사용할 수 없다(무마커 추정 경로 미연결)",
                retryable=False,
            )
        if self._bundle_dir is None:
            raise JobFailed("BACKGROUND_INCOMPLETE", "배경 묶음 폴더를 모른다", retryable=True)
        try:
            prepared = cbj.prepare_from_bundle(self._bundle_dir, prior)
            job_input = cbj.build_job_input(
                prepared=prepared, cal=prior,
                calibration_ref=f"s3://{PREFLIGHT_BUCKET}/{self._run_id}/calibration.json",
                calibration_hash=sha256(json.dumps(prior, sort_keys=True).encode()),
                image_uris={i.camera_id: f"s3://{PREFLIGHT_BUCKET}/{self._run_id}/{i.camera_id}.jpg" for i in prepared.images},
            )
        except cbj.CleanBgJobError as exc:
            raise JobFailed("BACKGROUND_INCOMPLETE", f"배경 묶음 검사 실패: {exc}", retryable=True) from exc
        by_cam = {i.camera_id: i for i in prepared.images}
        missing = [cid for cid in camera_ids if cid not in by_cam]
        if missing:
            raise JobFailed("BACKGROUND_INCOMPLETE", f"배경 묶음에 없는 카메라: {missing}", retryable=True)
        job_input["images"] = [im for im in job_input["images"] if im["camera_id"] in set(camera_ids)]
        (self.root / "images").mkdir(exist_ok=True)
        hashes = []
        for im in job_input["images"]:
            raw = by_cam[im["camera_id"]].raw_bytes
            hashes.append(sha256(raw))
            (self.root / "images" / f"{im['camera_id']}.image").write_bytes(raw)
        write_json(self.root / "calibration.json", prior)
        write_json(self.root / "job.json", {"input": job_input, "run_id": self._run_id, "image_hashes": hashes})
        if progress is not None:
            progress(1 / 5)

        self._run_stage("prepare")
        if progress is not None:
            progress(2 / 5)
        self._run_stage("mapanything")
        if progress is not None:
            progress(3 / 5)
        prepared_doc = load_prepared(self.root / "prepared")
        run = load_mapanything_run(self.root / "mapanything", prepared=prepared_doc)
        cams = {c["camera_id"]: c for c in prior["cameras"]}
        poses: dict[str, CameraPose] = {}
        for cid, T in run.cam2world().items():
            Rc = np.asarray(T, float)[:3, :3]
            poses[cid] = CameraPose(cid, Rc.T, -Rc.T @ np.asarray(T, float)[:3, 3])
        intrinsics = {
            cid: {
                "image_size": cams[cid]["image_size"], "K": cams[cid]["K"],
                "distortion_model": cams[cid]["distortion"]["model"],
                "distortion_coeffs": cams[cid]["distortion"]["coeffs"],
            }
            for cid in camera_ids if cid in cams
        }
        if progress is not None:
            progress(4 / 5)
        result = build_world_from_pointcloud_and_poses(
            site_id=site_id, camera_ids=camera_ids, camera_heights_m=camera_heights_m,
            height_tolerance_cm=height_tolerance_cm, points_m=run.all_points(), unscaled_poses=poses,
            intrinsics_by_cam=intrinsics, created_at_ms=created_at_ms,
        )
        if progress is not None:
            progress(1.0)
        return result

    # ------------------------------------------------------------------ (f) World 다시 맞추기
    def anchor_world(self, calibration_doc: Mapping[str, Any]) -> dict[str, Any]:
        """이전 채택 보정(Tier 0 활성 보정)의 World 로 옮긴 보정 문서. 이전 보정이 없으면 그대로(기록 없음).

        SIMAI_SETUP_WORLD_ANCHOR=off 면 옮기지 않고 DISABLED 기록만 남긴다."""
        from simai_ai.calibration.world_frame.reanchor import reanchor_calibration

        doc, result = reanchor_calibration(self._prior, calibration_doc, enabled=self._world_anchor_enabled)
        if result is not None:
            rec = result.to_record()
            log.info("작업 %s World 다시 맞추기: %s(%s) 기준 %s, yaw=%s rad, 이동=%s m, 축척=%s, 배치 잔차 최대=%s m, 방향 변화 최대=%s rad",
                     self.ctx.job_id, "채택" if result.accepted else "거부", result.reason,
                     result.reference_calibration_version, rec["yaw_rad"], rec["translation_m"], rec["scale"],
                     rec["max_center_residual_m"], rec["max_orientation_change_rad"])
        return doc

    # ------------------------------------------------------------------ (f)
    def publish(self, calibration_doc: Mapping[str, Any]) -> PublishedCalibration:
        """새 보정 문서를 작업 폴더에 두고(이후 preview·bake 의 World 정렬 기준) 객체 저장소에 올린다. 버전은 덮어쓰지 않는다."""
        doc = copy.deepcopy(dict(calibration_doc))
        version = str(doc["calibration_version"])
        # 세팅 출처·계보(계약 2.2.0, content_hash 제외 키). Tier 0 는 previous 가 자기 활성 보정일 때만 이 문서를 새 기준으로 채택한다
        doc["schema_version"] = "2.5.0" if "world_anchor" in doc else "2.2.0"  # world_anchor 는 계약 2.5.0
        doc["setup_session_id"] = str(self.ctx.input["setup_session_id"])
        if self._prior is not None:
            doc["previous_calibration_version"] = str(self._prior["calibration_version"])
        if (self.root / "calibration.json").is_file():
            (self.root / "calibration.prior.json").write_bytes((self.root / "calibration.json").read_bytes())
        write_json(self.root / "calibration.json", doc)
        if not (self.root / ALIGNMENT_REFERENCE_FILE).is_file():
            # 이 대여에서 처음 발행한 보정(Stage A)을 맵 정렬 기준으로 남긴다. 걷기 다듬기 보정(-760)은 같은 World 라 정렬은 이 기준으로 한다
            write_json(self.root / ALIGNMENT_REFERENCE_FILE, doc)
        raw = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            uri = self.store.put(self.ctx, f"calibration-{version}.json", raw, "application/json")
        except ObjectStoreError as exc:
            raise JobFailed("SETUP_OBJECT_FAILED", str(exc), retryable=exc.retryable) from exc
        return PublishedCalibration(version, ObjectRef.of(uri, raw))

    # ------------------------------------------------------------------ (g)
    def preview(self, *, setup_session_id: str, site_id: str, calibration_doc: Mapping[str, Any],
                backgrounds: CleanBackgroundBundle, progress: Callable[[float], None] | None = None) -> PreviewDraftResult:
        self._run_stage("preview")
        if progress is not None:
            progress(1 / 2)
        doc = self._read_stage_doc("preview.json", "preview")
        glb = next((a for a in doc["assets"] if a["kind"] == "GLTF_VISUAL"), None)
        if glb is None:
            raise JobFailed("STATIC_OUTPUT_INVALID", "미리보기 산출물에 GLB 가 없다", retryable=False)
        ref = self._upload_asset(glb)
        preview_ver = f"scene-preview-{self._run_id}"[:160]
        zhc = self._compute_zone_height_changes(
            calibration_doc=calibration_doc,
            compared_scene_version=preview_ver,
            glb_spec=glb,
        )
        if progress is not None:
            progress(1.0)
        return PreviewDraftResult(
            preview_scene_version=preview_ver,
            scene_asset=ref,
            floor_coverage=self._read_floor_coverage(),
            zone_height_changes=zhc,
        )

    def _load_previous_glb(self, prev_scene: Mapping[str, Any]) -> bytes | None:
        """이전 관제 Scene GLB 바이트를 한 번 내려받아 캐시한다. 실패해도 세팅을 멈추지 않고 None 을 돌려준다."""
        if self._prev_glb_loaded:
            return self._prev_glb_bytes
        self._prev_glb_loaded = True
        try:
            content = self.store.get(self.ctx, str(prev_scene["uri"]))
        except ObjectStoreError as exc:
            if self.ctx.cancelled.is_set():
                raise JobFailed("CANCELLED", "대여 취소 또는 시간 초과로 중단됨", retryable=True) from exc
            log.warning("작업 %s: 이전 관제 Scene GLB 를 내려받지 못해 높이 비교를 건너뛴다(%s)", self.ctx.job_id, exc)
            return None
        expected_sha = prev_scene.get("sha256")
        if expected_sha and hashlib.sha256(content).hexdigest() != str(expected_sha):
            log.warning("작업 %s: 이전 관제 Scene GLB 해시가 입력과 달라 높이 비교를 건너뛴다", self.ctx.job_id)
            return None
        self._prev_glb_bytes = content
        return content

    def _compute_zone_height_changes(
        self,
        *,
        calibration_doc: Mapping[str, Any],
        compared_scene_version: str,
        glb_spec: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """이전 관제 맵과 새 맵(미리보기·정식)의 구역별 상면 높이 차이를 계산한다(S15P31A101-630)."""
        if "previous_zones" not in self.ctx.input and "previous_scene" not in self.ctx.input:
            return None
        prev_zones = list(self.ctx.input.get("previous_zones") or [])
        prev_scene = self.ctx.input.get("previous_scene")
        prev_ver = (
            str(prev_scene["scene_version"])
            if isinstance(prev_scene, Mapping) and prev_scene.get("scene_version")
            else None
        )
        if not isinstance(prev_scene, Mapping) or not prev_scene.get("uri"):
            return build_unknown_zone_height_changes(
                prev_zones,
                reason=REASON_NO_PREVIOUS_SCENE,
                previous_scene_version=prev_ver,
                compared_scene_version=compared_scene_version,
            )
        anchor = calibration_doc.get("world_anchor")
        if not isinstance(anchor, Mapping) or anchor.get("accepted") is not True:
            return build_unknown_zone_height_changes(
                prev_zones,
                reason=REASON_WORLD_NOT_ANCHORED,
                previous_scene_version=prev_ver,
                compared_scene_version=compared_scene_version,
            )
        path = (self.root / glb_spec["path"]).resolve()
        try:
            curr_bytes = path.read_bytes()
        except OSError:
            return build_unknown_zone_height_changes(
                prev_zones,
                reason=REASON_SCENE_UNREADABLE,
                previous_scene_version=prev_ver,
                compared_scene_version=compared_scene_version,
            )
        prev_bytes = self._load_previous_glb(prev_scene)
        if prev_bytes is None:
            return build_unknown_zone_height_changes(
                prev_zones,
                reason=REASON_SCENE_UNREADABLE,
                previous_scene_version=prev_ver,
                compared_scene_version=compared_scene_version,
            )
        try:
            return evaluate_zone_height_changes(
                prev_bytes,
                curr_bytes,
                prev_zones,
                previous_scene_version=prev_ver,
                compared_scene_version=compared_scene_version,
            )
        except Exception as exc:  # noqa: BLE001 — 참고 표시라 세팅을 멈추지 않고 모름으로 둔다
            log.warning("작업 %s: 구역 높이 비교 실패 → 모름: %s", self.ctx.job_id, type(exc).__name__)
            return build_unknown_zone_height_changes(
                prev_zones,
                reason=REASON_SCENE_UNREADABLE,
                previous_scene_version=prev_ver,
                compared_scene_version=compared_scene_version,
            )

    def _read_floor_coverage(self) -> dict[str, Any] | None:
        """미리보기 단계가 쓴 바닥 감시 범위(S15P31A101-606). 없거나 읽지 못하거나 계약에 맞지 않으면 None(표시용이라 세팅은 이어간다)."""
        path = self.root / "floor_coverage.json"
        if not path.is_file():
            return None
        try:
            doc = read_json(path)
        except (OSError, ValueError) as exc:
            log.warning("작업 %s: 바닥 감시 범위를 읽지 못해 싣지 않는다: %s", self.ctx.job_id, type(exc).__name__)
            return None
        problems = floor_coverage_problems(doc)
        if problems:
            log.warning("작업 %s: 바닥 감시 범위가 계약에 맞지 않아 싣지 않는다: %s", self.ctx.job_id, problems[:3])
            return None
        return doc

    # ------------------------------------------------------------------ (g) 카메라 프레임
    def publish_frames(self, *, setup_session_id: str, camera_ids: Sequence[str],
                       backgrounds: CleanBackgroundBundle) -> list[CameraFrameRef]:
        """카메라별 사람 지운 배경 한 장을 JPEG(긴 변 1280 px 이하)로 올린다. 배경이 없는 카메라는 건너뛴다(모르면 비운다).

        같은 배경이면 같은 JPEG(sha256)가 나와 재시도해도 같은 객체다. 얼굴은 원칙상 없지만 `face_policy` 는 BLURRED 로 알려
        VLM 워커가 기존 얼굴 흐림 관문을 거치게 한다.
        """
        out: list[CameraFrameRef] = []
        for cid in list(camera_ids)[:MAX_CAMERA_FRAMES]:
            sources = backgrounds.frames_by_cam.get(cid) or []
            if not sources:
                continue
            jpeg = _frame_jpeg(sources[0])
            if jpeg is None:
                log.warning("작업 %s: 카메라 %s 의 배경을 JPEG 프레임으로 만들지 못해 건너뛴다", self.ctx.job_id, cid)
                continue
            try:
                uri = self.store.put(self.ctx, f"frame-{cid}.jpg", jpeg, "image/jpeg")
            except ObjectStoreError as exc:
                raise JobFailed("SETUP_OBJECT_FAILED", str(exc), retryable=exc.retryable) from exc
            out.append(CameraFrameRef(cid, ObjectRef.of(uri, jpeg)))
        return out

    # ------------------------------------------------------------------ (g) 굽기 입력 사진
    def publish_bake_images(self, *, setup_session_id: str, camera_ids: Sequence[str],
                            backgrounds: CleanBackgroundBundle) -> BakeImages | None:
        """미리보기·굽기가 읽는 `images/<카메라>.image` 를 카메라당 1장 올린다. 없거나 읽지 못한 카메라는 건너뛴다(모르면 비운다).

        `initialize` 가 배경 묶음 사진을 그대로 이 폴더에 썼고 prepare·preview·bake 단계가 이 파일을 읽으므로, 다른 시점의 사진이
        섞이지 않는다. 저장소 오류는 `SETUP_OBJECT_FAILED` 로 내고 처리기가 로그만 남긴 뒤 이어간다.
        """
        out: list[BakeImageRef] = []
        for cid in list(camera_ids)[:MAX_BAKE_IMAGES]:
            try:
                raw = (self.root / "images" / f"{cid}.image").read_bytes()
            except OSError:
                log.warning("작업 %s: 카메라 %s 의 굽기 입력 사진이 작업 폴더에 없어 건너뛴다", self.ctx.job_id, cid)
                continue
            jpeg = _bake_image_jpeg(raw)
            if jpeg is None:
                log.warning("작업 %s: 카메라 %s 의 굽기 입력 사진을 JPEG 로 만들지 못해 건너뛴다", self.ctx.job_id, cid)
                continue
            try:
                uri = self.store.put(self.ctx, f"bake-{cid}.jpg", jpeg, "image/jpeg")
            except ObjectStoreError as exc:
                raise JobFailed("SETUP_OBJECT_FAILED", str(exc), retryable=exc.retryable) from exc
            out.append(BakeImageRef(cid, ObjectRef.of(uri, jpeg)))
        return BakeImages(backgrounds.bundle_id, tuple(out)) if out else None

    # ------------------------------------------------------------------ (h)
    def bake(self, *, setup_session_id: str, site_id: str, calibration_doc: Mapping[str, Any],
             backgrounds: CleanBackgroundBundle, preview_scene_version: str,
             progress: Callable[[float], None] | None = None) -> BakedScene:
        summary = self.supplement() if self.ctx.input.get("supplement_media") else None
        if progress is not None:
            progress(1 / 4)
        if summary is not None and summary["status"] == "APPLIED":
            try:
                glb = self._bake_glb()
            except JobFailed as exc:
                if exc.code == "CANCELLED" or self.ctx.cancelled.is_set():
                    raise
                log.warning("작업 %s: 보조 촬영을 넣은 굽기 실패(%s) → 보조 촬영 없이 다시 굽는다", self.ctx.job_id, exc.code)
                sc.restore_baseline(self.root)
                summary = self._fallback_summary(summary, sc.R_BAKE_FAILED)
                glb = self._bake_glb()
        else:
            glb = self._bake_glb()
        sc.annotate_bake(self.root, summary)
        if progress is not None:
            progress(3 / 4)
        ref = self._upload_asset(glb)
        scene_ver = f"scene-{self._run_id}"[:160]
        zhc = self._compute_zone_height_changes(
            calibration_doc=calibration_doc,
            compared_scene_version=scene_ver,
            glb_spec=glb,
        )
        if progress is not None:
            progress(1.0)
        return BakedScene(scene_ver, ref, summary, zhc)

    def _bake_glb(self) -> Mapping[str, Any]:
        self._run_stage("bake")
        doc = self._read_stage_doc("bake.json", "bake")
        if doc.get("fallback_reason") and not doc.get("assets"):
            self._run_stage("fallback")
            doc = self._read_stage_doc("bake.json", "fallback")
        glbs = [a for a in doc["assets"] if a["kind"] == "GLTF_VISUAL"]
        if len(glbs) != 1:
            # 정식 Scene 은 GLB 하나다(EC2 가 scene_version 의 gltf_key 하나로 등록)
            raise JobFailed("STATIC_OUTPUT_INVALID", f"굽기 산출물의 GLB 가 하나가 아니다({len(glbs)})", retryable=False)
        return glbs[0]

    # ------------------------------------------------------------------ 보조 촬영(-605)
    @staticmethod
    def _fallback_summary(summary: Mapping[str, Any], reason: str) -> dict[str, Any]:
        out = dict(summary)
        out.update(status="FALLBACK", reason=reason)
        # 자리 잡았지만 굽기 실패·회수로 쓰지 않은 파일은 미등록(NOT_REGISTERED)과 구분한다(1.13.0, -652)
        out["media"] = [{**m, "status": sc.M_REGISTERED_NOT_USED} if m["status"] == sc.M_USED else m
                        for m in out.get("media", [])]
        out.pop("captured_from_ms", None)
        out.pop("captured_to_ms", None)
        if not out["media"]:
            out.pop("media")
        return out

    def _media_inputs(self) -> list[sc.MediaInput]:
        return [sc.MediaInput(media_id=str(m["media_id"]), kind=str(m["kind"]), path=None,
                              captured_at_ms=m.get("captured_at_ms"), source=m.get("source"), device=m.get("device"),
                              sha256=m.get("sha256"))
                for m in list(self.ctx.input.get("supplement_media") or [])[: sc.MAX_MEDIA]]

    def _skip(self, reason: str) -> dict[str, Any]:
        log.warning("작업 %s: 보조 촬영을 건너뛴다(%s)", self.ctx.job_id, reason)
        return sc.summary(status="SKIPPED", reason=reason, media=self._media_inputs())

    def _reclaimed_summary(self, summary: Mapping[str, Any] | None, exc: BakeReclaimed) -> dict[str, Any]:
        """다시 굽기 회수(-621) 요약. 보조 촬영 단계 전이면 SKIPPED, 그 뒤면 FALLBACK. 사유는 LIVE_WORK_STARTED."""
        stage = str(exc.args[0]) if exc.args else ""
        if summary is not None:
            return self._fallback_summary(summary, sc.R_LIVE_WORK_STARTED)
        status = "FALLBACK" if stage == "supplement" else "SKIPPED"
        return sc.summary(status=status, reason=sc.R_LIVE_WORK_STARTED, media=self._media_inputs())

    def supplement(self) -> dict[str, Any]:
        """보조 촬영 파일을 받고 `supplement` 단계를 돌려 요약을 돌려준다. 취소만 예외로 올리고 나머지는 요약으로 바꾼다."""
        job_path = self.root / "job.json"
        opts = (read_json(job_path)["input"].get("options") or {}) if job_path.is_file() else {}
        path = str(opts.get("reconstruction_path") or os.environ.get("SIMAI_RECONSTRUCTION_PATH") or "fallback1")
        if path.strip().lower() in SUPPLEMENT_UNSUPPORTED_PATHS:
            return self._skip(sc.R_PATH_UNSUPPORTED)
        remaining = self.ctx.remaining_s()
        if remaining is not None and remaining < SUPPLEMENT_MIN_REMAINING_S:
            return self._skip(sc.R_TIME_BUDGET)
        media = self._media_inputs()
        rows: list[dict[str, Any]] = []
        media_dir = self.root / sc.SUPPLEMENT_DIR / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        for m, raw_item in zip(media, self.ctx.input["supplement_media"]):
            row = m.provenance()
            try:
                content = self.store.get(self.ctx, str(raw_item["uri"]))
            except ObjectStoreError as exc:
                if self.ctx.cancelled.is_set():
                    raise JobFailed("CANCELLED", "대여 취소 또는 시간 초과로 중단됨", retryable=True) from exc
                log.warning("작업 %s: 보조 촬영 %s 를 받지 못함(%s)", self.ctx.job_id, m.media_id, exc)
                rows.append({**row, "status": sc.M_DOWNLOAD_FAILED})
                continue
            digest = hashlib.sha256(content).hexdigest()
            if (m.sha256 and digest != m.sha256) or not content or len(content) > SUPPLEMENT_MAX_MEDIA_BYTES:
                log.warning("작업 %s: 보조 촬영 %s 의 바이트가 입력(sha256·크기)과 다르다", self.ctx.job_id, m.media_id)
                rows.append({**row, "status": sc.M_DOWNLOAD_FAILED})
                continue
            suffix = ".video" if m.kind == "VIDEO" else ".image"
            (media_dir / f"{m.media_id}{suffix}").write_bytes(content)
            rows.append({**row, "path": f"media/{m.media_id}{suffix}"})
        sc.write_request(self.root, rows)
        try:
            self._run_stage("supplement")
        except JobFailed as exc:
            if exc.code == "CANCELLED":
                raise
            log.warning("작업 %s: 보조 촬영 단계 실패(%s) → 보조 촬영 없이 굽는다", self.ctx.job_id, exc.code)
            sc.restore_baseline(self.root)
            return sc.summary(status="FALLBACK", reason=sc.R_RECONSTRUCTION_FAILED, media=media)
        try:
            return dict(read_json(self.root / sc.SUPPLEMENT_DIR / sc.RESULT_FILE)["summary"])
        except (OSError, ValueError, KeyError):
            sc.restore_baseline(self.root)
            return sc.summary(status="FALLBACK", reason=sc.R_RECONSTRUCTION_FAILED, media=media)

    # ------------------------------------------------------------------ SUPPLEMENT_REBAKE(-605)
    def _get_verified(self, ref: Mapping[str, Any], what: str) -> bytes:
        try:
            content = self.store.get(self.ctx, str(ref["uri"]))
        except ObjectStoreError as exc:
            if self.ctx.cancelled.is_set():
                raise JobFailed("CANCELLED", "대여 취소 또는 시간 초과로 중단됨", retryable=True) from exc
            raise JobFailed("SETUP_OBJECT_FAILED", f"{what} 를 받지 못함: {exc}", retryable=exc.retryable) from exc
        if hashlib.sha256(content).hexdigest() != str(ref["sha256"]):
            raise JobFailed("INVALID_INPUT", f"{what} 의 sha256 이 입력과 다르다", retryable=False)
        return content

    def rebake(self, *, setup_session_id: str, site_id: str, camera_ids: Sequence[str],
               camera_heights_m: Mapping[str, float], height_tolerance_cm: float, rebake_base: Mapping[str, Any],
               on_height_checks: Callable[[dict[str, dict[str, Any]]], None]) -> RebakeOutcome:
        """기준 보정 문서·굽기 입력 사진으로 작업 폴더를 다시 만들고 보조 촬영이 적용될 때만 새 맵을 굽는다.

        배경 수집·내부 파라미터·Stage A·보정 등록·미리보기·구역 초안은 하지 않는다. prepare·mapanything(고정 카메라)은 보조 촬영
        관문의 기준(보조 촬영 없을 때 정렬·재투영)과 같은 입력 폴더를 만들려고 다시 돈다(기준 맵 자체는 다시 굽지 않는다)."""
        from simai_ai.calibration.extrinsics.joint_ba_v2 import compute_height_checks

        cal_raw = self._get_verified(rebake_base["calibration_document"], "기준 보정 문서")
        try:
            cal = json.loads(cal_raw.decode("utf-8"))
            cams = {str(c["camera_id"]): c for c in cal["cameras"]}
        except (ValueError, KeyError, TypeError) as exc:
            raise JobFailed("INVALID_INPUT", "기준 보정 문서를 읽지 못함", retryable=False) from exc
        if str(cal.get("calibration_version")) != str(rebake_base["calibration_version"]):
            raise JobFailed("INVALID_INPUT", "기준 보정 문서의 버전이 rebake_base.calibration_version 과 다르다",
                            retryable=False)
        missing = [cid for cid in camera_ids if cid not in cams]
        if missing:
            raise JobFailed("INVALID_INPUT", f"기준 보정 문서에 없는 카메라: {missing}", retryable=False)
        restored = {}
        for cid in camera_ids:
            R, tvec = np.asarray(cams[cid]["R"], float), np.asarray(cams[cid]["t"], float)
            restored[cid] = float((-R.T @ tvec)[2])
        height_checks = compute_height_checks(camera_heights_m, restored, tolerance_cm=height_tolerance_cm)
        on_height_checks(height_checks)
        images = {str(im["camera_id"]): im for im in rebake_base["bake_images"]["images"]}
        (self.root / "images").mkdir(exist_ok=True)
        hashes = []
        for cid in camera_ids:
            raw = self._get_verified(images[cid], f"카메라 {cid} 굽기 입력 사진")
            hashes.append(sha256(raw))
            (self.root / "images" / f"{cid}.image").write_bytes(raw)
        write_json(self.root / "calibration.json", cal)
        job_input = {
            "calibration_version": str(cal["calibration_version"]),
            "images": [{"camera_id": cid, "kind": "CLEAN_BACKGROUND"} for cid in camera_ids],
            "input_provenance": {"frames": {"source": "LIVE", "camera_ids": list(camera_ids)},
                                 "calibration": {"source": str(cal.get("provider_source") or "G1_AUTO"),
                                                 "calibration_version": str(cal["calibration_version"])},
                                 "is_upper_bound": False},
            "options": {},
        }
        write_json(self.root / "job.json", {
            "input": job_input, "run_id": self._run_id, "image_hashes": hashes,
            "rebake_base": {"calibration_version": str(rebake_base["calibration_version"]),
                            "scene_version": str(rebake_base["scene_version"])}})
        base_scene = str(rebake_base["scene_version"])
        self._rebaking = True
        summary: dict[str, Any] | None = None
        try:
            self._run_stage("prepare")
            self._run_stage("mapanything")
            summary = self.supplement()
            if summary["status"] != "APPLIED":
                return RebakeOutcome(base_scene, summary, height_checks)
            try:
                glb = self._bake_glb()
            except JobFailed as exc:
                if exc.code == "CANCELLED" or self.ctx.cancelled.is_set():
                    raise
                log.warning("작업 %s: 보조 촬영 다시 굽기 실패(%s) → 원래 맵 유지", self.ctx.job_id, exc.code)
                return RebakeOutcome(base_scene, self._fallback_summary(summary, sc.R_BAKE_FAILED), height_checks)
        except BakeReclaimed as exc:
            # 같은 현장 작업이 시작됐다(명세 3.2 5항): 자식 프로세스는 이미 끝났다. 실패가 아니라 원래 맵 유지로 끝낸다(-621)
            log.warning("작업 %s: 작업 시작으로 다시 굽기를 %s 단계에서 멈췄다(원래 맵 유지)", self.ctx.job_id, exc)
            return RebakeOutcome(base_scene, self._reclaimed_summary(summary, exc), height_checks)
        sc.annotate_bake(self.root, summary)
        ref = self._upload_asset(glb)
        scene_ver = f"scene-{self._run_id}"[:160]
        zhc = self._compute_zone_height_changes(
            calibration_doc=cal,
            compared_scene_version=scene_ver,
            glb_spec=glb,
        )
        return RebakeOutcome(scene_ver, summary, height_checks, ref, zhc)

    # ------------------------------------------------------------------ 공통
    def _run_stage(self, stage: str) -> None:
        # 정식 굽기(bake·fallback)와 그 안의 보조 촬영(supplement)은 작업 시작(LIVE) 신호로도 멈춘다(명세 3.2 5항, -597·-621).
        # 다시 굽기(SUPPLEMENT_REBAKE)는 READY_FOR_ZONES 뒤 오프라인 굽기라 prepare·mapanything 까지 모두 멈춘다. 자식 정리는 ProcessStages
        reclaimable = stage in _RECLAIMABLE_STAGES or (self._rebaking and stage in _REBAKE_RECLAIMABLE_STAGES)
        stop = _AnyEvent(self.ctx.cancelled, self.ctx.live_work) if reclaimable else self.ctx.cancelled
        try:
            self.stages.run(stage, self.root, timeout_s=STAGE_TIMEOUTS_S[stage], cancelled=stop)
        except ReconstructionError as exc:
            if exc.code == "CANCELLED" and reclaimable and self.ctx.live_work.is_set() and not self.ctx.cancelled.is_set():
                raise BakeReclaimed(stage) from exc
            if exc.code == "CANCELLED":
                raise JobFailed("CANCELLED", "대여 취소 또는 시간 초과로 중단됨", retryable=True) from exc
            retry = exc.code.endswith("_TIMEOUT") or exc.code.endswith("_TOOL_FAILED")
            raise JobFailed(exc.code, self._with_log_tail(stage, str(exc)), retryable=retry) from exc

    def _with_log_tail(self, stage: str, message: str) -> str:
        """실패 단계 로그 끝을 워커 로그(LOG_TAIL_LINES 줄)와 실패 메시지(끝 몇 줄)에 남긴다. 작업 폴더는 곧 지워진다."""
        tail = _log_tail(self.root / f"{stage}.log", LOG_TAIL_LINES)
        if not tail:
            return message
        log.warning("작업 %s site_setup %s 단계 실패, 로그 끝 %d줄:\n%s", self.ctx.job_id, stage, len(tail), "\n".join(tail))
        short = " / ".join(tail[-3:])[-_MESSAGE_TAIL_CHARS:]
        return f"{message[:_MESSAGE_TAIL_CHARS]} | {stage}.log 끝: {short}"

    def keep_failure_logs(self) -> Path | None:
        """실패한 작업의 단계 로그·오류 문서를 상태 폴더로 복사한다(작업 폴더는 지워진다). 실패해도 작업 결과에는 영향 없음."""
        files = [p for pat in ("*.log", "*.error.json") for p in sorted(self.root.glob(pat))
                 if p.is_file() and p.stat().st_size <= _KEEP_LOG_MAX_BYTES]
        if not files:
            return None
        dest = self.settings.state_dir / "worker" / FAILED_LOGS_DIR / self._run_id
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for f in files:
                shutil.copyfile(f, dest / f.name)
        except OSError as exc:
            log.warning("작업 %s site_setup 실패 로그를 남기지 못함: %s", self.ctx.job_id, type(exc).__name__)
            return None
        log.warning("작업 %s site_setup 실패 로그를 %s 에 남겼다", self.ctx.job_id, dest)
        return dest

    def _read_stage_doc(self, name: str, stage: str) -> dict[str, Any]:
        path = self.root / name
        if not path.is_file():
            raise JobFailed(f"STATIC_{stage.upper()}_EMPTY", "단계 결과 문서 없음", retryable=False)
        return read_json(path)

    def _upload_asset(self, spec: Mapping[str, Any]) -> ObjectRef:
        path = (self.root / spec["path"]).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file() or path.stat().st_size == 0:
            raise JobFailed("STATIC_OUTPUT_EMPTY", f"산출물 경로·내용 오류: {spec['path']}", retryable=False)
        content = path.read_bytes()
        try:
            return ObjectRef.of(self.store.put(self.ctx, path.name, content, str(spec["content_type"])), content)
        except ObjectStoreError as exc:
            raise JobFailed("SETUP_OBJECT_FAILED", str(exc), retryable=exc.retryable) from exc


class ProductionSiteSetupHandler:
    """대여마다 작업 폴더·단계 어댑터를 새로 만들어 `SiteSetupHandler` 를 돌린다(폴더는 끝나면 지운다)."""

    def __init__(
        self,
        store: ObjectStore,
        settings: AutoStaticSettings,
        stages: Stages,
        *,
        clock_ms: Callable[[], int],
        live_gate: Callable[[], bool] | None = None,
        live_notice: Callable[[], bool] | None = None,
        workspace: Path | None = None,
        object_cross_checker: Any = None,
        background_wait_s: float = DEFAULT_BACKGROUND_WAIT_S,
        background_poll_s: float = DEFAULT_BACKGROUND_POLL_S,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[Any, float], Any] | None = None,
        world_anchor_enabled: bool = True,
        camera_check_enabled: bool = True,
    ) -> None:
        self.store, self.settings, self.stages = store, settings, stages
        self._world_anchor_enabled = world_anchor_enabled
        self._camera_check_enabled = camera_check_enabled
        self._clock_ms, self._live_gate, self._live_notice = clock_ms, live_gate, live_notice
        self._workspace, self._cross = workspace, object_cross_checker
        self._wait = {"background_wait_s": background_wait_s, "background_poll_s": background_poll_s, "sleep": sleep}
        if monotonic is not None:
            self._wait["monotonic"] = monotonic

    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        try:
            with tempfile.TemporaryDirectory(prefix="site-setup-", dir=self._workspace) as tmp:
                s = ProductionSiteSetupStages(
                    ctx, self.store, self.settings, self.stages, Path(tmp), clock_ms=self._clock_ms,
                    world_anchor_enabled=self._world_anchor_enabled,
                )
                handler = SiteSetupHandler(
                    feature_matcher=match_sift,
                    background_collector=s.collect, world_initializer=s.initialize, calibration_publisher=s.publish,
                    preview_builder=s.preview, texture_baker=s.bake, frame_publisher=s.publish_frames,
                    bake_image_publisher=s.publish_bake_images, rebaker=s.rebake,
                    object_cross_checker=self._cross,
                    live_gate=self._live_gate, live_notice=self._live_notice, world_anchor=s.anchor_world,
                    clock_ms=self._clock_ms, **self._wait,
                    # 세팅 「카메라 확인」(-646): 같은 state_dir 의 Tier 0 가 관제 view 를 넘긴다
                    camera_check=CameraCheckRunner(self.settings.state_dir / "camera-check", clock_ms=self._clock_ms)
                    if self._camera_check_enabled else None,
                )
                try:
                    return handler(ctx)
                except Exception:
                    s.keep_failure_logs()
                    raise
                finally:
                    s.release_capture()
        except OSError as exc:
            raise JobFailed("SETUP_IO_FAILED", type(exc).__name__, retryable=True) from exc


# 작업 시작(LIVE) 신호로 멈추는 단계: 정식 굽기와 그 대체(-597), 굽기 안의 보조 촬영(-605 → -621).
# 일반 세팅의 READY_FOR_ZONES 전 단계(prepare·mapanything·preview)는 여기 넣지 않는다(서버가 그 전 작업 시작을 막는 것은 -621 범위 밖)
_RECLAIMABLE_STAGES = frozenset({"bake", "fallback", "supplement"})
# SUPPLEMENT_REBAKE 에서 더 멈추는 단계: 기준 입력 폴더를 다시 만드는 고정 카메라 복원(-621)
_REBAKE_RECLAIMABLE_STAGES = frozenset({"prepare", "mapanything"})


class _AnyEvent:
    """둘 중 하나라도 켜지면 켜진 것으로 보는 읽기 전용 신호. ProcessStages.run 이 쓰는 is_set·wait 만 있다."""

    __slots__ = ("_events",)

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        self._events[0].wait(timeout)  # 첫 신호(취소)만 깨어 기다린다. 나머지는 다음 확인(0.05 s 주기)에서 본다
        return self.is_set()
