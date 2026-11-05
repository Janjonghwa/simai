"""`site_setup` 운영 단계 어댑터(S15P31A101-465): 같은 Pod 의 Tier 0 배경 묶음·MapAnything·미리보기·굽기를 `SiteSetupHandler` 주입점에 잇는다.

한 작업(대여)마다 임시 작업 폴더 하나(`root`)를 쓰고, 기존 정적 3D 단계 도구(`simai_ai.scene.reconstruct_tasks` 의
prepare·mapanything·preview·bake·fallback)를 `Stages`(기본 `ProcessStages`, 취소·시간 제한 지킴)로 돌린다. 그 폴더 레이아웃은
`static_reconstruct` 와 같다(images/·calibration.json·job.json). 그래서 -451 미리보기 맵·굽기 코드를 그대로 쓴다.

- (a) `collect`: Tier 0 가 내보낸 사람 지운 배경 묶음(SIMAI_STATE_DIR/clean-background)을 읽는다. 아직 덜 됐으면 `status.json` 의
  카메라별 표본 수로 수집률(완성 전 최대 99)을 돌려주고, 처리기가 `SIMAI_SETUP_BACKGROUND_WAIT_S`(기본 240 s)까지 다시 본다
  (그래도 안 되면 재시도 가능한 `BACKGROUND_INCOMPLETE`). 묶음의 마지막 관측 시각이 보정 문서 created_at 이 된다(S15P31A101-473).
- (b) 내부 파라미터: Tier 0 활성 보정 문서의 K·왜곡을 재사용한다. 없으면 `INTRINSICS_UNAVAILABLE` 로 끝낸다(무마커 추정 경로는 이 처리기에 아직
  연결하지 않았다, 지어내지 않는다).
- (c)(d) `initialize`: prepare → mapanything 단계 → `build_world_from_pointcloud_and_poses`(바닥 Z=0, 벽 +X, 모서리 원점, 카메라 높이 축척).
- (f) `publish`: 새 보정 문서를 작업 폴더에 두고 객체 저장소에 올린다. 올린 객체 참조(주소·sha256·크기)를 함께 돌려주면 처리기가
  `artifacts.calibration_document` 로 알리고, EC2 가 그 문서를 읽어 새 calibration_version 으로 등록한다(S15P31A101-467).
- (g) `preview`: reconstruct_tasks `preview` 단계 → GLB 업로드 → `scene-preview-<run>`(+ `artifacts.preview_scene`).
- (g) `publish_frames`: 사람 지운 배경에서 카메라별 대표 프레임 1장(JPEG, 긴 변 1280 px 이하)을 같은 산출물 경로로 올리고 카메라 ID·sha256 과
  함께 `artifacts.camera_frames` 로 알린다. EC2 가 이를 AI 구역 초안(`vlm_zone_draft`) 입력 `frames` 에 싣는다(S15P31A101-479).
- (h) `bake`: reconstruct_tasks `bake`(도구 부재 시 `fallback`) → GLB 업로드 → `scene-<run>`(+ `artifacts.scene`).

단계가 실패하면 그 단계 로그(`<단계>.log`)의 끝 줄을 워커 로그와 실패 메시지에 남기고, 작업 폴더를 지우기 전에 로그·오류 문서를
`SIMAI_STATE_DIR/worker/site-setup-failed/<작업>-<대여>/` 에 복사한다(S15P31A101-473).
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.scene.reconstruct import (
    ReconstructionError,
    Stages,
    read_json,
    sha256,
    write_json,
)

from .auto_static import AutoStaticSettings, calibration_document_path
from simai_ai.calibration.extrinsics.feature_matching import match_sift
from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError
from .site_setup import (
    MAX_CAMERA_FRAMES,
    BakedScene,
    CameraFrameRef,
    CameraPose,
    CleanBackgroundBundle,
    InitialWorldReconstruction,
    ObjectRef,
    PreviewDraftResult,
    PublishedCalibration,
    SiteSetupHandler,
    build_world_from_pointcloud_and_poses,
)

PREFLIGHT_BUCKET = "preflight"
STAGE_TIMEOUTS_S = {"prepare": 180.0, "mapanything": 1200.0, "preview": 60.0, "bake": 1800.0, "fallback": 120.0}
# Tier 0 묶음은 카메라마다 최소 20장 × 5 s ≈ 100 s 걸린다(services/tier0/settings.py CleanBackgroundSettings).
# 기다림 기본 240 s = 그 두 배 남짓 + 묶음 조립·사람이 오래 가린 카메라 여유. 실행 상한(서버 900 s) 안에서 복원·굽기 시간을 남긴다
DEFAULT_BACKGROUND_WAIT_S = 240.0
DEFAULT_BACKGROUND_POLL_S = 2.0
TIER0_MIN_SAMPLES_PER_CAMERA = 20  # Tier 0 기본값. 수집률 표시에만 쓴다(완성 판정은 Tier 0 의 ready·묶음 COMPLETE)
FRAME_MAX_LONG_SIDE_PX = 1280  # 구역 초안용 프레임 긴 변 상한(VLM 크롭에 충분, 업로드는 작게)
FRAME_JPEG_QUALITY = 90
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


def _partial_collection_pct(settings: AutoStaticSettings, camera_ids: Sequence[str], prior_version: str | None) -> dict[str, float]:
    """묶음이 완성되기 전 카메라별 수집률(Tier 0 status.json 의 표본 수). 완성 전이라 최대 99. 다른 보정의 수집이면 0."""
    zeros = {cid: 0.0 for cid in camera_ids}
    try:
        status = json.loads((settings.clean_background_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return zeros
    if not isinstance(status, dict) or not isinstance(status.get("cameras"), dict):
        return zeros
    if prior_version is not None and status.get("calibration_version") != prior_version:
        return zeros
    out = {}
    for cid in camera_ids:
        cam = status["cameras"].get(cid)
        if not isinstance(cam, dict):
            out[cid] = 0.0
            continue
        if cam.get("ready") is True or cam.get("exported") is True:
            out[cid] = 99.0  # 카메라는 됐고 묶음 조립을 기다린다
            continue
        n = cam.get("sample_count")
        ok = isinstance(n, int) and not isinstance(n, bool) and n > 0
        out[cid] = round(min(99.0, 100.0 * n / TIER0_MIN_SAMPLES_PER_CAMERA), 2) if ok else 0.0
    return out


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


def _bundle_dir(settings: AutoStaticSettings) -> tuple[Path, str] | None:
    root = settings.clean_background_dir
    try:
        status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(status, dict) or status.get("complete") is not True:
        return None
    version, bundle = status.get("calibration_version"), status.get("bundle")
    if not isinstance(version, str) or not isinstance(bundle, str):
        return None
    path = root / version / Path(bundle).name
    return (path, version) if (path / "COMPLETE").is_file() else None


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
    ) -> None:
        self.ctx, self.store, self.settings, self.stages, self.root = ctx, store, settings, stages, root
        self._clock_ms = clock_ms
        self._load_calibration = calibration_loader
        self._prior: dict[str, Any] | None = None
        self._bundle_dir: Path | None = None
        self._run_id = f"{ctx.job_id}-{ctx.lease_id}"
        self._baked: dict[str, Any] = {}

    # ------------------------------------------------------------------ (a)
    def collect(self, *, setup_session_id: str, site_id: str, camera_ids: Sequence[str]) -> CleanBackgroundBundle:
        found = _bundle_dir(self.settings)
        if found is None:
            prior = self._load_calibration(self.settings)
            prior_version = None if prior is None else str(prior.get("calibration_version"))
            return CleanBackgroundBundle("", _partial_collection_pct(self.settings, camera_ids, prior_version))
        path, version = found
        try:
            meta = read_json(path / "clean_backgrounds.json")
            cameras = meta["cameras"]
        except (OSError, ValueError, KeyError):
            return CleanBackgroundBundle(path.name, {cid: 0.0 for cid in camera_ids})
        pct: dict[str, float] = {}
        frames: dict[str, list[Path]] = {}
        for cid in camera_ids:
            entry = cameras.get(cid) if isinstance(cameras, dict) else None
            image = None if not isinstance(entry, dict) else entry.get("image")
            ok = isinstance(entry, dict) and entry.get("ready") is True and isinstance(image, str) and (path / image).is_file()
            pct[cid] = 100.0 if ok else 0.0
            if ok:
                frames[cid] = [path / image]
        self._bundle_dir = path
        prior = self._load_calibration(self.settings)
        if prior is not None and prior.get("calibration_version") != version:
            # 묶음과 활성 보정이 다르면 그 묶음의 내부 파라미터·해상도를 믿을 수 없다. 새 묶음을 기다린다.
            return CleanBackgroundBundle(path.name, {cid: 0.0 for cid in camera_ids})
        self._prior = prior
        return CleanBackgroundBundle(path.name, pct, frames, observed_until_ms=_observed_until_ms(path, meta))

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

        self._run_stage("prepare")
        self._run_stage("mapanything")
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
        return build_world_from_pointcloud_and_poses(
            site_id=site_id, camera_ids=camera_ids, camera_heights_m=camera_heights_m,
            height_tolerance_cm=height_tolerance_cm, points_m=run.all_points(), unscaled_poses=poses,
            intrinsics_by_cam=intrinsics, created_at_ms=created_at_ms,
        )

    # ------------------------------------------------------------------ (f)
    def publish(self, calibration_doc: Mapping[str, Any]) -> PublishedCalibration:
        """새 보정 문서를 작업 폴더에 두고(이후 preview·bake 의 World 정렬 기준) 객체 저장소에 올린다. 버전은 덮어쓰지 않는다."""
        doc = copy.deepcopy(dict(calibration_doc))
        version = str(doc["calibration_version"])
        # 세팅 출처·계보(계약 2.2.0, content_hash 제외 키). Tier 0 는 previous 가 자기 활성 보정일 때만 이 문서를 새 기준으로 채택한다
        doc["schema_version"] = "2.2.0"
        doc["setup_session_id"] = str(self.ctx.input["setup_session_id"])
        if self._prior is not None:
            doc["previous_calibration_version"] = str(self._prior["calibration_version"])
        if (self.root / "calibration.json").is_file():
            (self.root / "calibration.prior.json").write_bytes((self.root / "calibration.json").read_bytes())
        write_json(self.root / "calibration.json", doc)
        raw = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            uri = self.store.put(self.ctx, f"calibration-{version}.json", raw, "application/json")
        except ObjectStoreError as exc:
            raise JobFailed("SETUP_OBJECT_FAILED", str(exc), retryable=exc.retryable) from exc
        return PublishedCalibration(version, ObjectRef.of(uri, raw))

    # ------------------------------------------------------------------ (g)
    def preview(self, *, setup_session_id: str, site_id: str, calibration_doc: Mapping[str, Any],
                backgrounds: CleanBackgroundBundle) -> PreviewDraftResult:
        self._run_stage("preview")
        doc = self._read_stage_doc("preview.json", "preview")
        glb = next((a for a in doc["assets"] if a["kind"] == "GLTF_VISUAL"), None)
        if glb is None:
            raise JobFailed("STATIC_OUTPUT_INVALID", "미리보기 산출물에 GLB 가 없다", retryable=False)
        ref = self._upload_asset(glb)
        return PreviewDraftResult(preview_scene_version=f"scene-preview-{self._run_id}"[:160], scene_asset=ref)

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

    # ------------------------------------------------------------------ (h)
    def bake(self, *, setup_session_id: str, site_id: str, calibration_doc: Mapping[str, Any],
             backgrounds: CleanBackgroundBundle, preview_scene_version: str) -> BakedScene:
        self._run_stage("bake")
        doc = self._read_stage_doc("bake.json", "bake")
        if doc.get("fallback_reason"):
            self._run_stage("fallback")
            doc = self._read_stage_doc("bake.json", "fallback")
        glbs = [a for a in doc["assets"] if a["kind"] == "GLTF_VISUAL"]
        if len(glbs) != 1:
            # 정식 Scene 은 GLB 하나다(EC2 가 scene_version 의 gltf_key 하나로 등록)
            raise JobFailed("STATIC_OUTPUT_INVALID", f"굽기 산출물의 GLB 가 하나가 아니다({len(glbs)})", retryable=False)
        ref = self._upload_asset(glbs[0])
        return BakedScene(f"scene-{self._run_id}"[:160], ref)

    # ------------------------------------------------------------------ 공통
    def _run_stage(self, stage: str) -> None:
        try:
            self.stages.run(stage, self.root, timeout_s=STAGE_TIMEOUTS_S[stage], cancelled=self.ctx.cancelled)
        except ReconstructionError as exc:
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
    ) -> None:
        self.store, self.settings, self.stages = store, settings, stages
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
                )
                handler = SiteSetupHandler(
                    feature_matcher=match_sift,
                    background_collector=s.collect, world_initializer=s.initialize, calibration_publisher=s.publish,
                    preview_builder=s.preview, texture_baker=s.bake, frame_publisher=s.publish_frames,
                    object_cross_checker=self._cross,
                    live_gate=self._live_gate, live_notice=self._live_notice, clock_ms=self._clock_ms, **self._wait,
                )
                try:
                    return handler(ctx)
                except Exception:
                    s.keep_failure_logs()
                    raise
        except OSError as exc:
            raise JobFailed("SETUP_IO_FAILED", type(exc).__name__, retryable=True) from exc
