"""`site_setup` 운영 단계 어댑터(S15P31A101-465): 같은 Pod 의 Tier 0 배경 묶음·MapAnything·미리보기·굽기를 `SiteSetupHandler` 주입점에 잇는다.

한 작업(대여)마다 임시 작업 폴더 하나(`root`)를 쓰고, 기존 정적 3D 단계 도구(`simai_ai.scene.reconstruct_tasks` 의
prepare·mapanything·preview·bake·fallback)를 `Stages`(기본 `ProcessStages`, 취소·시간 제한 지킴)로 돌린다. 그 폴더 레이아웃은
`static_reconstruct` 와 같다(images/·calibration.json·job.json). 그래서 -451 미리보기 맵·굽기 코드를 그대로 쓴다.

- (a) `collect`: Tier 0 가 내보낸 사람 지운 배경 묶음(SIMAI_STATE_DIR/clean-background)을 읽는다. 없거나 덜 됐으면 수집률 0 으로
  돌려 처리기가 재시도 가능한 `BACKGROUND_INCOMPLETE` 로 끝낸다.
- (b) 내부 파라미터: Tier 0 활성 보정 문서의 K·왜곡을 재사용한다. 없으면 `INTRINSICS_UNAVAILABLE` 로 끝낸다(무마커 추정 경로는 이 처리기에 아직
  연결하지 않았다, 지어내지 않는다).
- (c)(d) `initialize`: prepare → mapanything 단계 → `build_world_from_pointcloud_and_poses`(바닥 Z=0, 벽 +X, 모서리 원점, 카메라 높이 축척).
- (f) `publish`: 새 보정 문서를 작업 폴더에 두고 객체 저장소에 올린다. 올린 객체 참조(주소·sha256·크기)를 함께 돌려주면 처리기가
  `artifacts.calibration_document` 로 알리고, EC2 가 그 문서를 읽어 새 calibration_version 으로 등록한다(S15P31A101-467).
- (g) `preview`: reconstruct_tasks `preview` 단계 → GLB 업로드 → `scene-preview-<run>`(+ `artifacts.preview_scene`).
- (h) `bake`: reconstruct_tasks `bake`(도구 부재 시 `fallback`) → GLB 업로드 → `scene-<run>`(+ `artifacts.scene`).
"""

from __future__ import annotations

import copy
import json
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
from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError
from .site_setup import (
    BakedScene,
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
            return CleanBackgroundBundle("", {cid: 0.0 for cid in camera_ids})
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
        return CleanBackgroundBundle(path.name, pct, frames)

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
            raise JobFailed(exc.code, str(exc), retryable=retry) from exc

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
        workspace: Path | None = None,
        object_cross_checker: Any = None,
    ) -> None:
        self.store, self.settings, self.stages = store, settings, stages
        self._clock_ms, self._live_gate = clock_ms, live_gate
        self._workspace, self._cross = workspace, object_cross_checker

    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        try:
            with tempfile.TemporaryDirectory(prefix="site-setup-", dir=self._workspace) as tmp:
                s = ProductionSiteSetupStages(
                    ctx, self.store, self.settings, self.stages, Path(tmp), clock_ms=self._clock_ms,
                )
                handler = SiteSetupHandler(
                    background_collector=s.collect, world_initializer=s.initialize, calibration_publisher=s.publish,
                    preview_builder=s.preview, texture_baker=s.bake, object_cross_checker=self._cross,
                    live_gate=self._live_gate, clock_ms=self._clock_ms,
                )
                return handler(ctx)
        except OSError as exc:
            raise JobFailed("SETUP_IO_FAILED", type(exc).__name__, retryable=True) from exc
