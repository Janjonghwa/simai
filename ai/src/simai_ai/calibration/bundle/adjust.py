"""걷는 사람 keypoint 번들 조정 (명세 4.3 3단계 정밀화, 4단계 검산 일부, 요구사항 FR-23).

입력: 카메라별 2D keypoint 시퀀스(같은 사람 대응·공통 시각 정렬 끝남), 내부 파라미터 초기값(-116
`BundleAdjustmentInit`), 외부 파라미터 초기값(MapAnything 초기 추정. 시험에서는 교란한 정답), 선택으로 실측 정물(-100).
출력: `HumanBundleResult` — 카메라별 K·왜곡·R·t(계약 camera_calibration), 판정 근거(바닥 평면 차이·커버리지·스케일
근거), CCVS 방식 카메라 높이 검산, 퇴화 표시, input_provenance 를 담은 기록.

- 판정은 하지 않는다. CALIBRATED/PROVISIONAL 은 `CalibrationVersionStore.propose`(criteria)만 정한다.
  `propose_to` 는 조정이 실패·불확실이면 제안하지 않을 뿐, 판정을 바꾸거나 건너뛰지 않는다.
- 모르면 비운다: 스케일 근거(실측 정물·실측 카메라 높이)가 없으면 스케일은 외부 파라미터 초기값(MapAnything 메트릭)을
  그대로 이어받고 확정하지 않았다고 기록한다(scale_estimates 비움, 문서 scale_basis 는 MODEL_ESTIMATE). 2대 미만에서 보인
  관절은 변수로 만들지 않는다. 바닥 평면을 맞출 접지점이 부족한 카메라가 있으면 바닥 평면 차이를 비운다.
- 렌즈 모드(-321, `BundleConfig.lens_mode`): per_camera(기본, 카메라별 f·k1·k2), shared(4대 공통 f·k1·k2 + 카메라별 R·t),
  fixed(렌즈 고정, R·t 만). 풀이는 `solver` 모듈 설명.
- 실측 카메라 높이 축척(-321, `measured_camera_heights`, 명세 4.3 스케일 근거 MEASURED_CAMERA_HEIGHT): 조정이 끝난 뒤
  카메라별 (실측 / 조정 높이) 비의 중앙값을 낸다(`checks.camera_height_scale`: 2대 이상, 카메라끼리 3% 안일 때만).
  실측 정물 제약이 없으면 그 배율로 World 전체(카메라 중심·점·뼈 길이)를 원점·바닥 Z=0 기준으로 늘리고, 근거를
  scale_estimates 에 넣는다(문서 scale_basis MEASURED_CAMERA_HEIGHT, CALIBRATED 주 근거는 아님). 사람 관측만으로는
  축척이 게이지(재투영이 바뀌지 않음)라 조정 뒤 배율과 같다. 실측 정물이 있으면 정물이 축척을 정하고(우선순위가 높다)
  카메라 높이는 교차 확인 근거로만 넣는다. 쓰지 못하면 이유를 기록하고 축척을 바꾸지 않는다.
- 출처: 입력이 시뮬레이터·정답(input_provenance.is_upper_bound)이면 문서 출처를 SIM·DATASET_GT 로 둔다. 그 결과는
  계약상 CALIBRATED 가 될 수 없고 지표는 상한이다(명세 20.4 규칙 3·4). 상한 입력이 아니면(LIVE·FILE·공개 데이터 영상
  DATASET 등) G1_AUTO 다. 공개 데이터 영상으로 돌린 결과는 우리 현장 보정이 아니므로 버전 저장소에 넣지 않는다(호출자 책임).
- HumanCalib 코드는 가져오지 않았다(원리만 따른 자체 구현: 사람 관절을 점으로 둔 번들 조정, 뼈 길이 일관성, 바닥 접지).
  HumanCalib 의 사람 선택·3D lifting 초기화는 쓰지 않는다. 초기 3D 는 초기 보정으로 삼각측량한다.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
from scipy.spatial.transform import Rotation

from simai_ai.calibration.bundle import checks, solver
from simai_ai.calibration.bundle.inputs import BundleConfig, MeasuredObjectObservation, PersonObservation
from simai_ai.calibration.bundle.skeleton import (
    BONE_NAMES,
    FEET,
    KEYPOINT_NOISE_SCALE,
    L_ANKLE,
    L_SHOULDER,
    R_ANKLE,
    R_SHOULDER,
    bones_for,
)
from simai_ai.calibration.criteria import CalibrationEvidence, HeightCheckpoint, ScaleBasis, ScaleEstimate
from simai_ai.calibration.document import CameraCalibration
from simai_ai.calibration.intrinsics.record import BundleAdjustmentInit
from simai_ai.calibration.versions import CalibrationVersionStore, ProposalResult
from simai_ai.geometry import distortion
from simai_ai.geometry.rig import RigCamera
from simai_ai.geometry.triangulation import triangulate_point
from simai_ai.providers.contract import check_input_provenance
from simai_ai.tracking.time_model import require_ms

BUNDLE_RECORD_FORMAT = "simai.human_bundle_adjustment"
BUNDLE_RECORD_VERSION = "0.2.0"  # 0.2.0: lens, scale.camera_heights, config.lens_mode (-321)
PROVIDER_VERSION = "simai-human-bundle-0.2.0"  # 0.2.0: 렌즈 모드·실측 카메라 높이 축척 (-321)

STATUS_CONVERGED = "CONVERGED"
STATUS_UNCERTAIN = "UNCERTAIN"
STATUS_FAILED = "FAILED"

# 퇴화·불확실 표시
FLAG_TOO_FEW_CAMERAS = "TOO_FEW_CAMERAS"
FLAG_TOO_FEW_POINTS = "TOO_FEW_POINTS"
FLAG_FEW_CONTACTS = "FEW_FLOOR_CONTACTS"
FLAG_CONTACTS_COLLINEAR = "FLOOR_CONTACTS_COLLINEAR"
FLAG_LOW_IMAGE_COVERAGE = "LOW_IMAGE_COVERAGE"
FLAG_NOT_CONVERGED = "NOT_CONVERGED"
FLAG_TOO_MANY_OUTLIERS = "TOO_MANY_OUTLIERS"
FLAG_FLOOR_PLANE_UNKNOWN = "FLOOR_PLANE_UNKNOWN"
FLAG_HEIGHT_CHECK_MISMATCH = "CAMERA_HEIGHT_CHECK_MISMATCH"
FLAG_HEIGHT_CHECK_UNKNOWN = "CAMERA_HEIGHT_CHECK_UNKNOWN"
# 기록용(불확실로 만들지 않음)
NOTE_SCALE_NOT_DETERMINED = "SCALE_NOT_DETERMINED"
NOTE_FLOOR_FROM_ANKLES = "FLOOR_FROM_ANKLES"
NOTE_CAMERA_HEIGHT_REJECTED = "CAMERA_HEIGHT_SCALE_REJECTED"

MIN_POINTS = 50
# 라운드별 수렴 기준(받아들인 걸음의 상대 비용 감소)
ROUGH_TOLERANCE = 1e-5
FINAL_TOLERANCE = 1e-8
DEFAULT_CONFIG = BundleConfig()


class BundleNotUsable(ValueError):
    """실패·불확실한 조정 결과를 보정 버전 후보로 넣으려 했다."""


@dataclass(frozen=True)
class HumanBundleResult:
    status: str
    flags: tuple[str, ...]
    notes: tuple[str, ...]
    provider_source: str
    cameras: tuple[CameraCalibration, ...]
    floor_plane_disagreement_m: float | None
    floor_plane_camera_ids: tuple[str, ...]
    floor_coverage_ratio: float | None
    scale_estimates: tuple[ScaleEstimate, ...]
    scale_determined: bool
    camera_height_checks: tuple[checks.CameraHeightCheck, ...]
    record: Mapping[str, Any] = field(repr=False)

    @property
    def fallback_scale_basis(self) -> ScaleBasis:
        """스케일 근거가 없을 때 문서 scale_basis: 외부 파라미터 초기값(MapAnything 메트릭)을 이어받았다."""
        return ScaleBasis.MODEL_ESTIMATE

    def evidence(self, checkpoints: Sequence[HeightCheckpoint] = ()) -> CalibrationEvidence:
        """판정 입력. 독립 검산점(4.4, 키를 아는 사람이 선 3~5곳)은 이 조정 밖(-115·-152)에서 받아 넣는다."""
        return CalibrationEvidence(
            floor_coverage_ratio=self.floor_coverage_ratio,
            floor_plane_disagreement_m=self.floor_plane_disagreement_m,
            floor_plane_camera_ids=self.floor_plane_camera_ids,
            scale_estimates=self.scale_estimates,
            checkpoints=tuple(checkpoints),
        )

    def propose_to(
        self,
        store: CalibrationVersionStore,
        *,
        at_ms: int,
        checkpoints: Sequence[HeightCheckpoint] = (),
        world_axes_source: str | None = None,
    ) -> ProposalResult:
        """보정 버전 후보로 넣는다. 판정·전환은 저장소(criteria)가 한다. 실패·불확실한 조정은 넣지 않는다.
        world_axes_source 는 World 축을 정한 근거(-102 결과의 axes_source)이고 모르면 None(문서에 싣지 않음)이다."""
        if self.status != STATUS_CONVERGED:
            raise BundleNotUsable(
                f"번들 조정이 {self.status} 다 ({', '.join(self.flags)}). 걷기 재수행·정물 추가가 필요하다"
            )
        return store.propose(
            provider_source=self.provider_source,
            provider_version=PROVIDER_VERSION,
            cameras=self.cameras,
            evidence=self.evidence(checkpoints),
            at_ms=at_ms,
            fallback_scale_basis=self.fallback_scale_basis,
            world_axes_source=world_axes_source,
        )


# --------------------------------------------------------------------------- 진입점


def adjust_human_bundle(
    observations: Sequence[PersonObservation],
    intrinsics: BundleAdjustmentInit,
    initial_extrinsics: Mapping[str, tuple[Sequence[Sequence[float]], Sequence[float]]],
    *,
    input_provenance: Mapping[str, Any],
    created_at: int,
    measured_objects: Sequence[MeasuredObjectObservation] = (),
    measured_camera_heights: Mapping[str, float] | None = None,
    config: BundleConfig = DEFAULT_CONFIG,
) -> HumanBundleResult:
    """번들 조정 한 번. initial_extrinsics 는 camera_id → (R, t) (OpenCV World → 카메라).

    measured_camera_heights: camera_id → 줄자로 잰 바닥 → 렌즈 중심 높이(m). 주면 축척 근거로 쓴다(모듈 설명).
    """
    require_ms("created_at", created_at)
    problems = check_input_provenance(input_provenance)
    if problems:
        raise ValueError(f"input_provenance 가 계약에 맞지 않는다: {problems}")
    provider_source = _provider_source(input_provenance)

    seen = {cid for obs in observations for cid in obs.views}
    init_ids = {c.camera_id for c in intrinsics.cameras}
    camera_ids = tuple(sorted(seen & init_ids & set(initial_extrinsics)))
    base = {
        "format": BUNDLE_RECORD_FORMAT,
        "format_version": BUNDLE_RECORD_VERSION,
        "created_at": created_at,
        "input_provenance": dict(input_provenance),
        "provider_source": provider_source,
        "method": "HUMAN_KEYPOINT_BUNDLE_ADJUSTMENT",
        "method_note": "HumanCalib 원리(뼈 길이 일관성·바닥 접지)를 따른 자체 구현. HumanCalib 코드·3D lifter 미사용",
        "camera_ids": list(camera_ids),
        "skipped_camera_ids": sorted((seen | init_ids | set(initial_extrinsics)) - set(camera_ids)),
    }
    if len(camera_ids) < 2:
        return _failed(provider_source, base, [FLAG_TOO_FEW_CAMERAS], "카메라 2대 미만")

    cams = tuple(_camera_init(intrinsics, initial_extrinsics, cid, config.fixed_lens) for cid in camera_ids)
    rigs0 = {c.camera_id: _rig(c.camera_id, c.image_size, c.R0, c.C0, c.f0, c.principal_point, c.k0) for c in cams}
    built = _build_problem(observations, measured_objects, cams, rigs0, config)
    if built.problem.n_points < MIN_POINTS:
        return _failed(provider_source, base, [FLAG_TOO_FEW_POINTS], f"2대 이상에서 보인 점 {built.problem.n_points}개")

    problem = built.problem
    history = []
    # 조정 → 이상치 제거 → 재조정. 마지막 라운드만 엄격한 수렴 기준으로 끝낸다
    solution, _ = solver.solve(
        problem, config.max_iterations, tolerance=FINAL_TOLERANCE if config.rounds == 1 else ROUGH_TOLERANCE
    )
    history.append(_round_record(problem, solution))
    initial_observations = len(problem.obs_cam)
    # 이상치 제거 전 기준의 카메라별 재투영 오차(1차 풀이, 모든 관측). 판정 지표(제거 뒤)와 함께 기록한다
    before_removal = _per_camera_errors(problem, solution)
    for r in range(1, config.rounds):
        # 관절별 잡음 배율만큼 기준을 넓힌다(엉덩이 κ 는 눈의 4.3배). 배율이 없으면 모두 outlier_px
        keep = solution.reprojection_errors(problem) <= config.outlier_px * problem.scales
        rebased = solver.rebase(problem, solution)
        dropped, old_of_new = rebased.drop_observations(keep)
        if dropped.n_points >= MIN_POINTS:
            built = built.remap(old_of_new)
            rebased = dropped
        problem = rebased
        solution, _ = solver.solve(
            problem,
            config.max_iterations,
            solver.start_vector(problem, solution),
            tolerance=FINAL_TOLERANCE if r == config.rounds - 1 else ROUGH_TOLERANCE,
        )
        history.append(_round_record(problem, solution))

    outliers = {
        "initial_observations": initial_observations,
        "inlier_observations": len(problem.obs_cam),
        "removed_observations": initial_observations - len(problem.obs_cam),
        "removed_ratio": round(1.0 - len(problem.obs_cam) / initial_observations, 4) if initial_observations else None,
        "max_removed_ratio": config.max_outlier_ratio,
        "outlier_px": config.outlier_px,
        "joint_noise_scaling": config.joint_noise_scaling,
    }
    solution, height_scale = _camera_height_scale(problem, solution, measured_camera_heights, config)
    return _finish(
        problem, solution, built, cams, config, provider_source, base, history, outliers, before_removal, height_scale
    )


@dataclass(frozen=True)
class _HeightScale:
    """실측 카메라 높이 축척 결과. scale_after 는 최종 World 에서 본 (실측 / 조정 높이) 중앙값(적용했으면 1, 못 쓰면 None)."""

    record: Mapping[str, Any]
    scale_after: float | None
    applied: bool
    camera_count: int


def _camera_height_scale(
    problem: solver.Problem,
    solution: solver.Solution,
    measured: Mapping[str, float] | None,
    config: BundleConfig,
) -> tuple[solver.Solution, _HeightScale | None]:
    """실측 카메라 높이 비의 중앙값을 내고, 실측 정물 제약이 없으면 World 를 그 배율로 늘린다(모듈 설명)."""
    if measured is None:
        return solution, None
    adjusted = {c.camera_id: float(c.C[2]) for c in solution.cameras}
    scale, record = checks.camera_height_scale(
        adjusted,
        measured,
        min_cameras=config.camera_height_min_cameras,
        max_spread=config.camera_height_max_spread,
        floor_known=not problem.fix_floor_to_initial,
    )
    record = dict(record)
    if scale is None:
        record["applied"] = False
        return solution, _HeightScale(record, None, False, 0)
    if len(problem.object_a) > 0:
        record["applied"] = False
        record["not_applied_reason"] = "실측 정물 제약이 축척을 정했다(우선순위가 높다). 교차 확인 근거로만 쓴다"
        return solution, _HeightScale(record, scale, False, int(record["cameras"]))
    scaled = _scale_solution(solution, scale)
    record["applied"] = True
    record["applied_scale"] = round(scale, 6)
    record["adjusted_height_after_m"] = {c.camera_id: round(float(c.C[2]), 4) for c in scaled.cameras}
    return scaled, _HeightScale(record, 1.0, True, int(record["cameras"]))


def _scale_solution(solution: solver.Solution, s: float) -> solver.Solution:
    """World 원점 기준 닮음 확대(회전 그대로). 바닥 Z=0·원점·벽 방향은 그대로이고 재투영은 바뀌지 않는다."""
    cams = tuple(replace(c, C=c.C * s) for c in solution.cameras)
    return replace(solution, cameras=cams, points=solution.points * s, bone_lengths=solution.bone_lengths * s)


def _per_camera_errors(problem: solver.Problem, solution: solver.Solution) -> dict[str, dict[str, float | None]]:
    errors = solution.reprojection_errors(problem)
    out = {}
    for i, cam in enumerate(solution.cameras):
        e = errors[problem.obs_cam == i]
        e = e[np.isfinite(e)]
        out[cam.camera_id] = {
            "median": _r(float(np.median(e))) if len(e) else None,
            "p95": _r(float(np.percentile(e, 95))) if len(e) else None,
            "observations": int(np.sum(problem.obs_cam == i)),
        }
    return out


# --------------------------------------------------------------------------- 문제 만들기


@dataclass(frozen=True)
class _Built:
    problem: solver.Problem
    # 점마다 ("person", 표본 번호, 관절) 또는 ("object", 정물 번호, 0=꼭대기/1=바닥)
    point_keys: tuple[tuple[str, int, int], ...]
    samples: tuple[PersonObservation, ...]
    bone_vars: tuple[tuple[str, str], ...]  # (person_id, 뼈 이름)
    floor_reference: Mapping[str, int]  # 접지 기준 keypoint(TOE/ANKLE)별 접지점 수
    objects: tuple[MeasuredObjectObservation, ...]

    def remap(self, old_of_new: np.ndarray) -> _Built:
        return _Built(
            self.problem,
            tuple(self.point_keys[i] for i in old_of_new),
            self.samples,
            self.bone_vars,
            self.floor_reference,
            self.objects,
        )


def _camera_init(
    intr: BundleAdjustmentInit,
    extr: Mapping[str, Any],
    camera_id: str,
    fixed_lens: tuple[float, float, float] | None = None,
) -> solver.CameraInit:
    """초기값. fixed_lens(fixed 모드)를 주면 초점·왜곡을 그 값으로 바꾼다(주점은 기록 그대로)."""
    init = intr.camera(camera_id)
    cam = init.camera
    K = np.asarray(cam["K"], dtype=float)
    model, coeffs = cam["distortion"]["model"], list(cam["distortion"]["coeffs"])
    if model == distortion.MODEL_NONE:
        k0 = (0.0, 0.0)
    elif model == distortion.MODEL_PINHOLE and all(c == 0.0 for c in coeffs[2:]):
        k0 = (float(coeffs[0]) if coeffs else 0.0, float(coeffs[1]) if len(coeffs) > 1 else 0.0)
    else:
        raise ValueError(f"{camera_id}: 번들 조정은 OPENCV_PINHOLE 방사 2계수(k1, k2)만 다룬다: {model} {coeffs}")
    f0 = float((K[0, 0] + K[1, 1]) / 2)
    if fixed_lens is not None:
        f0, k0 = float(fixed_lens[0]), (float(fixed_lens[1]), float(fixed_lens[2]))
    R, t = extr[camera_id]
    R = np.asarray(R, dtype=float)
    t = np.asarray(t, dtype=float)
    width, height = cam["image_size"]
    return solver.CameraInit(
        camera_id=camera_id,
        image_size=(int(width), int(height)),
        R0=R,
        C0=-R.T @ t,
        f0=f0,
        principal_point=(float(K[0, 2]), float(K[1, 2])),
        k0=k0,
        focal_std_px=float(init.focal_prior_std_px),
        k_std=(float(init.k_prior_std[0]), float(init.k_prior_std[1])),
    )


def _rig(camera_id, image_size, R, C, f, pp, k) -> RigCamera:
    R = np.asarray(R, dtype=float)
    return RigCamera.from_calibration(
        {
            "camera_id": camera_id,
            "image_size": list(image_size),
            "K": [[f, 0.0, pp[0]], [0.0, f, pp[1]], [0.0, 0.0, 1.0]],
            "distortion": {"model": distortion.MODEL_PINHOLE, "coeffs": [k[0], k[1], 0.0, 0.0, 0.0]},
            "R": R.tolist(),
            "t": (-R @ np.asarray(C, dtype=float)).tolist(),
        }
    )


def _usable(kp: Any, min_conf: float) -> bool:
    return kp is not None and len(kp) >= 3 and all(math.isfinite(float(v)) for v in kp[:3]) and kp[2] >= min_conf


def _init_point(
    views: Mapping[str, tuple[float, float]],
    rigs: Mapping[str, RigCamera],
    projectors: Mapping[str, solver.CameraSolution],
    config: BundleConfig,
) -> tuple[np.ndarray, dict[str, tuple[float, float]]] | None:
    """2대 이상 관측으로 초기 3D 를 삼각측량한다. 초기 재투영 오차가 큰 관측은 뺀다. 2대 미만이 되면 None.

    재투영 오차는 원본 픽셀에서 정방향 투영(왜곡 적용)으로 잰다(왜곡 제거를 한 번 더 하지 않으려고).
    """
    views = {cid: uv for cid, uv in views.items() if cid in rigs}
    for _ in range(len(views)):
        if len(views) < 2:
            return None
        ids = sorted(views)
        cams = [rigs[cid] for cid in ids]
        pixels = np.array([views[cid] for cid in ids], dtype=float)
        X = triangulate_point(cams, pixels)
        if X is None:
            return None
        proj = np.vstack([projectors[cid].project(X) for cid in ids])
        err = np.linalg.norm(proj - pixels, axis=1)
        err = np.where(np.isfinite(err), err, np.inf)
        worst = int(np.argmax(err))
        if err[worst] <= config.init_outlier_px:
            return X, views
        views = {cid: uv for cid, uv in views.items() if cid != ids[worst]}
    return None


def _build_problem(
    observations: Sequence[PersonObservation],
    objects: Sequence[MeasuredObjectObservation],
    cams: Sequence[solver.CameraInit],
    rigs: Mapping[str, RigCamera],
    config: BundleConfig,
) -> _Built:
    cam_index = {c.camera_id: i for i, c in enumerate(cams)}
    samples = tuple(sorted(observations, key=lambda o: (o.person_id, o.t)))
    points: list[np.ndarray] = []
    keys: list[tuple[str, int, int]] = []
    obs_cam: list[int] = []
    obs_pt: list[int] = []
    obs_uv: list[tuple[float, float]] = []
    obs_scale: list[float] = []
    index: dict[tuple[int, int], int] = {}

    projectors = {
        c.camera_id: solver.CameraSolution(c.camera_id, c.image_size, c.R0, c.C0, c.f0, c.principal_point, c.k0)
        for c in cams
    }

    def add_point(key, views) -> int | None:
        init = _init_point(views, rigs, projectors, config)
        if init is None:
            return None
        X, used = init
        p = len(points)
        points.append(X)
        keys.append(key)
        # 사람 관절은 관절별 잡음 배율, 정물 끝점은 1
        scale = KEYPOINT_NOISE_SCALE[key[2]] if key[0] == "person" and config.joint_noise_scaling else 1.0
        for cid, uv in used.items():
            obs_cam.append(cam_index[cid])
            obs_pt.append(p)
            obs_uv.append(uv)
            obs_scale.append(scale)
        return p

    joint_count = 17
    for s, sample in enumerate(samples):
        counts = {len(kps) for kps in sample.views.values()}
        joint_count = max(joint_count, *counts) if counts else joint_count
        for j in range(max(counts, default=0)):
            views = {
                cid: (float(kps[j][0]), float(kps[j][1]))
                for cid, kps in sample.views.items()
                if _usable(kps[j], config.min_keypoint_confidence)
            }
            p = add_point(("person", s, j), views)
            if p is not None:
                index[(s, j)] = p

    object_rows: list[tuple[int, int, float]] = []
    for o, obj in enumerate(objects):
        ends = []
        for e in range(2):
            views = {cid: tuple(map(float, pts[e])) for cid, pts in obj.endpoints.items()}
            ends.append(add_point(("object", o, e), views))
        if ends[0] is not None and ends[1] is not None:
            object_rows.append((ends[0], ends[1], obj.length_m))

    # 뼈: 사람별 뼈 길이 변수. 쓰는 행이 있는 변수만 만든다
    bone_rows: list[tuple[int, int, tuple[str, str]]] = []
    for s, sample in enumerate(samples):
        for name, a, b in bones_for(joint_count):
            if (s, a) in index and (s, b) in index:
                bone_rows.append((index[(s, a)], index[(s, b)], (sample.person_id, name)))
    bone_vars = tuple(sorted({v for _, _, v in bone_rows}, key=lambda v: (v[0], BONE_NAMES.index(v[1]))))
    var_index = {v: i for i, v in enumerate(bone_vars)}
    P = np.array(points).reshape(-1, 3)
    lengths0 = np.zeros(len(bone_vars))
    by_var: dict[int, list[float]] = defaultdict(list)
    for a, b, v in bone_rows:
        by_var[var_index[v]].append(float(np.linalg.norm(P[a] - P[b])))
    for i, ds in by_var.items():
        lengths0[i] = float(np.median(ds))

    contacts, floor_reference = _contacts(samples, index, P, config)

    problem = solver.Problem(
        cameras=tuple(cams),
        points0=P,
        obs_cam=np.array(obs_cam, dtype=int),
        obs_pt=np.array(obs_pt, dtype=int),
        obs_uv=np.array(obs_uv, dtype=float).reshape(-1, 2),
        bone_a=np.array([a for a, _, _ in bone_rows], dtype=int),
        bone_b=np.array([b for _, b, _ in bone_rows], dtype=int),
        bone_var=np.array([var_index[v] for _, _, v in bone_rows], dtype=int),
        bone_len0=lengths0,
        contact_pt=np.array([p for p, _ in contacts], dtype=int),
        contact_h=np.array([h for _, h in contacts], dtype=float),
        object_a=np.array([a for a, _, _ in object_rows], dtype=int),
        object_b=np.array([b for _, b, _ in object_rows], dtype=int),
        object_len=np.array([L for _, _, L in object_rows], dtype=float),
        pixel_sigma=config.pixel_sigma_px,
        bone_sigma=config.bone_sigma_m,
        floor_sigma=config.floor_sigma_m,
        object_sigma_rel=config.measured_object_sigma_rel,
        gauge_sigma=config.gauge_sigma,
        fix_scale_to_initial=not object_rows,
        fix_floor_to_initial=len(contacts) < 3,
        obs_scale=np.array(obs_scale, dtype=float) if config.joint_noise_scaling else None,
        lens_mode=config.lens_mode,
    )
    return _Built(problem, tuple(keys), samples, bone_vars, floor_reference, tuple(objects))


def _contacts(
    samples: Sequence[PersonObservation],
    index: Mapping[tuple[int, int], int],
    P: np.ndarray,
    config: BundleConfig,
) -> tuple[list[tuple[int, float]], dict[str, int]]:
    """접지한 발 keypoint (점 번호, 접지 높이)와 기준 keypoint 별 개수. 발마다 발 끝점이 있으면 발 끝, 없으면 발목을 쓴다.

    같은 사람의 이웃 표본(stance_window_ms 안)과 비교한 같은 발의 수평 속도가 stance_max_speed_mps 이하이면 접지로
    본다. 이웃 표본이 없으면 두 발 중 (높이 − 접지 높이)가 낮은 발만 접지로 본다.
    """
    heights = {"TOE": config.toe_contact_height_m, "ANKLE": config.ankle_contact_height_m}

    def foot_point(s: int, ankle: int, toe: int) -> tuple[int, str] | None:
        if (s, toe) in index:
            return index[(s, toe)], "TOE"
        if (s, ankle) in index:
            return index[(s, ankle)], "ANKLE"
        return None

    by_person: dict[str, list[int]] = defaultdict(list)
    for s, sample in enumerate(samples):
        by_person[sample.person_id].append(s)
    out: list[tuple[int, float]] = []
    counts = {"TOE": 0, "ANKLE": 0}
    for idxs in by_person.values():
        for pos, s in enumerate(idxs):
            candidates = []  # (점, 기준, 속도 또는 None)
            for ankle, toe in FEET:
                fp = foot_point(s, ankle, toe)
                if fp is None:
                    continue
                speed = math.inf
                for q in (pos - 1, pos + 1):
                    if not 0 <= q < len(idxs):
                        continue
                    n = idxs[q]
                    dt_ms = abs(samples[n].t - samples[s].t)
                    other = foot_point(n, ankle, toe)
                    if other is None or other[1] != fp[1] or not 0 < dt_ms <= config.stance_window_ms:
                        continue
                    d = float(np.linalg.norm(P[other[0], :2] - P[fp[0], :2]))
                    speed = min(speed, d / (dt_ms / 1000.0))
                candidates.append((fp[0], fp[1], speed if math.isfinite(speed) else None))
            if any(c[2] is not None for c in candidates):
                chosen = [c for c in candidates if c[2] is not None and c[2] <= config.stance_max_speed_mps]
            elif candidates:
                chosen = [min(candidates, key=lambda c: P[c[0], 2] - heights[c[1]])]
            else:
                chosen = []
            for p, ref, _ in chosen:
                out.append((p, heights[ref]))
                counts[ref] += 1
    return out, counts


# --------------------------------------------------------------------------- 결과


def _round_record(problem: solver.Problem, solution: solver.Solution) -> dict[str, Any]:
    err = solution.reprojection_errors(problem)
    return {
        "observations": len(err),
        "points": int(problem.n_points),
        "contacts": len(problem.contact_pt),
        "cost": round(solution.cost, 6),
        "residual_evaluations": solution.nfev,
        "message": solution.message,
        "solver_status": solution.status,
        "success": solution.success,
        "reprojection_median_px": _r(float(np.median(err))) if len(err) else None,
    }


def _finish(
    problem: solver.Problem,
    solution: solver.Solution,
    built: _Built,
    cams0: Sequence[solver.CameraInit],
    config: BundleConfig,
    provider_source: str,
    base: dict[str, Any],
    history: list[dict[str, Any]],
    outliers: dict[str, Any],
    before_removal: Mapping[str, Mapping[str, Any]],
    height_scale: _HeightScale | None = None,
) -> HumanBundleResult:
    flags: list[str] = []
    ratio = outliers["removed_ratio"]
    if ratio is not None and ratio > config.max_outlier_ratio:
        flags.append(FLAG_TOO_MANY_OUTLIERS)
    notes: list[str] = []
    errors = solution.reprojection_errors(problem)
    rigs = {
        c.camera_id: _rig(c.camera_id, c.image_size, c.R, c.C, c.f, c.principal_point, c.k) for c in solution.cameras
    }

    camera_docs: list[CameraCalibration] = []
    camera_records: list[dict[str, Any]] = []
    for i, (cam, cam0) in enumerate(zip(solution.cameras, cams0, strict=True)):
        e = errors[problem.obs_cam == i]
        uv = problem.obs_uv[problem.obs_cam == i]
        median = float(np.median(e)) if len(e) else None
        p95 = float(np.percentile(e, 95)) if len(e) else None
        coverage = checks.image_coverage(uv, cam.image_size)
        if coverage < config.min_image_coverage:
            flags.append(f"{FLAG_LOW_IMAGE_COVERAGE}:{cam.camera_id}")
        camera_docs.append(
            CameraCalibration(
                camera_id=cam.camera_id,
                image_size=cam.image_size,
                K=cam.K.tolist(),
                R=cam.R.tolist(),
                t=cam.t.tolist(),
                distortion_model=distortion.MODEL_PINHOLE,
                distortion_coeffs=cam.distortion_coeffs,
                reprojection_median_px=None if median is None else _r(median),
                reprojection_p95_px=None if p95 is None else _r(p95),
            )
        )
        rot = float(np.degrees(np.linalg.norm(Rotation.from_matrix(cam0.R0.T @ cam.R).as_rotvec())))
        camera_records.append(
            {
                "camera_id": cam.camera_id,
                "observations": len(e),
                "reprojection_error_px": {"median": _r(median), "p95": _r(p95)},
                "reprojection_error_before_outlier_removal_px": dict(before_removal.get(cam.camera_id, {})),
                "image_coverage": round(coverage, 3),
                "camera_height_m": round(float(cam.C[2]), 4),
                "focal_px": round(cam.f, 3),
                "k": [round(cam.k[0], 6), round(cam.k[1], 6)],
                "change_from_initial": {
                    "rotation_deg": round(rot, 4),
                    "center_m": round(float(np.linalg.norm(cam.C - cam0.C0)), 4),
                    "focal_px": round(cam.f - cam0.f0, 3),
                },
            }
        )

    # 접지점과 바닥 평면
    obs_by_point: dict[int, dict[str, tuple[float, float]]] = defaultdict(dict)
    for c, p, uv in zip(problem.obs_cam, problem.obs_pt, problem.obs_uv):
        obs_by_point[int(p)][solution.cameras[int(c)].camera_id] = (float(uv[0]), float(uv[1]))
    contact_xy = solution.points[problem.contact_pt, :2] if len(problem.contact_pt) else np.zeros((0, 2))
    contacts = [
        (obs_by_point[int(p)], float(h), (float(solution.points[p, 0]), float(solution.points[p, 1])))
        for p, h in zip(problem.contact_pt, problem.contact_h)
    ]
    planes = checks.floor_planes(rigs, contacts, config.min_plane_points)
    if planes.disagreement_m is None:
        flags.append(FLAG_FLOOR_PLANE_UNKNOWN)
    spread = checks.contact_minor_spread(contact_xy)
    if len(problem.contact_pt) < config.min_contacts:
        flags.append(FLAG_FEW_CONTACTS)
    if spread is None or spread < config.min_contact_minor_spread_m:
        flags.append(FLAG_CONTACTS_COLLINEAR)
    if built.floor_reference.get("ANKLE"):
        notes.append(NOTE_FLOOR_FROM_ANKLES)
    coverage_ratio = checks.floor_coverage(config.walkable_area_xy, contact_xy, config.coverage_cell_m)

    # CCVS 방식 카메라 높이 검산
    height_checks = _height_checks(built, problem, solution, rigs, obs_by_point)
    for hc in height_checks:
        diff = hc.difference_m
        if diff is None:
            flags.append(f"{FLAG_HEIGHT_CHECK_UNKNOWN}:{hc.camera_id}")
        elif diff > config.max_height_check_diff_m:
            flags.append(f"{FLAG_HEIGHT_CHECK_MISMATCH}:{hc.camera_id}")

    # 스케일
    scale_estimates: list[ScaleEstimate] = []
    object_records = []
    for a, b, L in zip(problem.object_a, problem.object_b, problem.object_len):
        obj = built.objects[built.point_keys[int(a)][1]]
        adjusted = float(np.linalg.norm(solution.points[a] - solution.points[b]))
        # 정물별 배율은 치수 제약이 들어간 끝점이 아니라, 최종 카메라로 관측 픽셀을 다시 삼각측량한 길이로 잰다.
        # 제약에 흡수된 치수 오기가 정물끼리의 일치 검사(criteria)를 빠져나가지 않게 하려는 것이다.
        measured = _retriangulated_length(obj, rigs)
        record_obj = {"label": obj.label, "length_m": float(L), "adjusted_length_m": round(adjusted, 4)}
        record_obj["retriangulated_length_m"] = None if measured is None else round(measured, 4)
        object_records.append(record_obj)
        if measured is not None and measured > 0:
            scale_estimates.append(ScaleEstimate(ScaleBasis.MEASURED_OBJECT, float(L) / measured, label=obj.label))
    # 스케일은 정물 치수 제약이 조정에 들어갔거나 실측 카메라 높이 배율을 적용했을 때만 정해진다
    # (배율 기록은 다시 삼각측량할 수 있는 정물만)
    if height_scale is not None and height_scale.scale_after is not None:
        scale_estimates.append(
            ScaleEstimate(
                ScaleBasis.MEASURED_CAMERA_HEIGHT, height_scale.scale_after, sample_count=height_scale.camera_count
            )
        )
    elif height_scale is not None:
        notes.append(NOTE_CAMERA_HEIGHT_REJECTED)
    if len(problem.object_a) > 0:
        scale_source = "MEASURED_OBJECT"
    elif height_scale is not None and height_scale.applied:
        scale_source = "MEASURED_CAMERA_HEIGHT"
    else:
        scale_source = "INITIAL_EXTRINSICS"
    scale_determined = scale_source != "INITIAL_EXTRINSICS"
    if not scale_determined:
        notes.append(NOTE_SCALE_NOT_DETERMINED)

    if len(solution.cameras) < config.min_cameras:
        flags.append(FLAG_TOO_FEW_CAMERAS)
    if not solution.success:
        flags.append(FLAG_NOT_CONVERGED)
    status = STATUS_UNCERTAIN if flags else STATUS_CONVERGED

    bones = defaultdict(dict)
    for (person, name), L in zip(built.bone_vars, solution.bone_lengths):
        bones[person][name] = round(float(L), 4)
    record = dict(base)
    record.update(
        {
            "status": status,
            "flags": flags,
            "notes": notes,
            "is_upper_bound": bool(base["input_provenance"].get("is_upper_bound")),
            "cameras": camera_records,
            "floor": {
                "reference_keypoint_counts": dict(built.floor_reference),
                "contact_count": len(problem.contact_pt),
                "contact_minor_spread_m": None if spread is None else round(spread, 4),
                "plane_disagreement_m": _r(planes.disagreement_m),
                "max_plane_offset_from_floor_m": _r(planes.max_offset_from_floor_m),
                "plane_point_counts": dict(planes.point_counts),
                "coverage_ratio": None if coverage_ratio is None else round(coverage_ratio, 4),
                # 접지 높이 가정(발 끝·발목)의 사람·신발별 편차만큼 Z=0 이 위아래로 치우칠 수 있다(예상 편차)
                "contact_height_uncertainty_m": (
                    config.ankle_contact_height_uncertainty_m
                    if built.floor_reference.get("ANKLE")
                    else config.toe_contact_height_uncertainty_m
                ),
            },
            "outliers": outliers,
            "lens": _lens_record(solution, config),
            "scale": {
                "determined": scale_determined,
                "source": scale_source,
                "measured_objects": object_records,
                "camera_heights": None if height_scale is None else dict(height_scale.record),
            },
            "camera_height_check": [hc.to_record() for hc in height_checks],
            "bone_lengths_m": dict(bones),
            "rounds": history,
            "config": {
                "pixel_sigma_px": config.pixel_sigma_px,
                "outlier_px": config.outlier_px,
                "joint_noise_scale": list(KEYPOINT_NOISE_SCALE) if config.joint_noise_scaling else None,
                "bone_sigma_m": config.bone_sigma_m,
                "floor_sigma_m": config.floor_sigma_m,
                "toe_contact_height_m": config.toe_contact_height_m,
                "ankle_contact_height_m": config.ankle_contact_height_m,
                "lens_mode": config.lens_mode,
            },
        }
    )
    return HumanBundleResult(
        status=status,
        flags=tuple(flags),
        notes=tuple(notes),
        provider_source=provider_source,
        cameras=tuple(camera_docs),
        floor_plane_disagreement_m=_r(planes.disagreement_m),
        floor_plane_camera_ids=tuple(c.camera_id for c in solution.cameras),
        floor_coverage_ratio=None if coverage_ratio is None else round(coverage_ratio, 4),
        scale_estimates=tuple(scale_estimates),
        scale_determined=scale_determined,
        camera_height_checks=tuple(height_checks),
        record=record,
    )


def _lens_record(solution: solver.Solution, config: BundleConfig) -> dict[str, Any]:
    """렌즈 모드와 (shared 면) 공통 값, (fixed 면) 고정 값의 출처."""
    out: dict[str, Any] = {"mode": config.lens_mode}
    if config.lens_mode == solver.LENS_SHARED and solution.cameras:
        c = solution.cameras[0]
        out["shared"] = {"focal_px": round(c.f, 3), "k": [round(c.k[0], 6), round(c.k[1], 6)]}
    elif config.lens_mode == solver.LENS_FIXED:
        out["fixed_from"] = "CONFIG" if config.fixed_lens is not None else "INTRINSICS_INIT"
        out["fixed_lens"] = None if config.fixed_lens is None else [float(v) for v in config.fixed_lens]
    return out


def _retriangulated_length(obj: MeasuredObjectObservation, rigs: Mapping[str, RigCamera]) -> float | None:
    """정물 끝점 두 개를 조정 결과 카메라로 (치수 제약 없이) 다시 삼각측량한 길이. 2대 미만이거나 실패하면 None."""
    ids = sorted(cid for cid in obj.endpoints if cid in rigs)
    if len(ids) < 2:
        return None
    cams = [rigs[cid] for cid in ids]
    ends = []
    for e in range(2):
        X = triangulate_point(cams, np.array([obj.endpoints[cid][e] for cid in ids], dtype=float))
        if X is None:
            return None
        ends.append(X)
    return float(np.linalg.norm(ends[0] - ends[1]))


def _height_checks(
    built: _Built,
    problem: solver.Problem,
    solution: solver.Solution,
    rigs: Mapping[str, RigCamera],
    obs_by_point: Mapping[int, Mapping[str, tuple[float, float]]],
) -> list[checks.CameraHeightCheck]:
    point_of = {key: i for i, key in enumerate(built.point_keys) if key[0] == "person"}
    joints = (L_SHOULDER, R_SHOULDER, L_ANKLE, R_ANKLE)
    per_sample = []
    for s in range(len(built.samples)):
        ids = [point_of.get(("person", s, j)) for j in joints]
        if any(i is None for i in ids):
            continue
        X = solution.points[ids]
        top, bottom = X[:2].mean(axis=0), X[2:].mean(axis=0)
        per_sample.append((s, ids, float(top[2] - bottom[2]), float(bottom[2])))
    # 사람별 어깨–발목 높이 차·발목 높이 중앙값
    by_person: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for s, _, H, zb in per_sample:
        by_person[built.samples[s].person_id].append((H, zb))
    medians = {
        p: (float(np.median([v[0] for v in vs])), float(np.median([v[1] for v in vs]))) for p, vs in by_person.items()
    }
    out = []
    for cid, rig in rigs.items():
        pairs = []
        for s, ids, _, _ in per_sample:
            views = [obs_by_point[i].get(cid) for i in ids]
            if any(v is None for v in views):
                continue
            H, zb = medians[built.samples[s].person_id]
            top = tuple(np.mean([views[0], views[1]], axis=0))
            bottom = tuple(np.mean([views[2], views[3]], axis=0))
            pairs.append((top, bottom, H, zb))
        out.append(checks.camera_height_check(rig, pairs))
    return out


def _failed(provider_source: str, base: dict[str, Any], flags: list[str], reason: str) -> HumanBundleResult:
    record = dict(base)
    record.update({"status": STATUS_FAILED, "flags": flags, "reason": reason})
    return HumanBundleResult(
        status=STATUS_FAILED,
        flags=tuple(flags),
        notes=(),
        provider_source=provider_source,
        cameras=(),
        floor_plane_disagreement_m=None,
        floor_plane_camera_ids=(),
        floor_coverage_ratio=None,
        scale_estimates=(),
        scale_determined=False,
        camera_height_checks=(),
        record=record,
    )


def _provider_source(provenance: Mapping[str, Any]) -> str:
    """상한 입력(시뮬레이터·정답)이 섞이면 그 출처, 아니면 G1_AUTO."""
    if not provenance.get("is_upper_bound"):
        return "G1_AUTO"
    sources = [provenance.get(k, {}).get("source") for k in ("frames", "calibration", "tracks")]
    return "SIM" if "SIM" in sources else "DATASET_GT"


def _r(v: float | None) -> float | None:
    return None if v is None else round(float(v), 4)
