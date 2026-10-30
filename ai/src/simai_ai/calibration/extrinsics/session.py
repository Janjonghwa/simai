"""회차 녹화의 다중인 연관과 고정 렌즈 번들 조정을 교대로 수행하고 직전 World에 정렬한다(-331, -343)."""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
from scipy.spatial.transform import Rotation

from simai_ai.calibration.bundle import BundleConfig, adjust_human_bundle, checks
from simai_ai.calibration.bundle.adjust import (
    FLAG_CONTACTS_COLLINEAR, FLAG_FEW_CONTACTS, FLAG_FLOOR_PLANE_UNKNOWN,
    FLAG_HEIGHT_CHECK_MISMATCH, FLAG_HEIGHT_CHECK_UNKNOWN,
)
from simai_ai.calibration.criteria import judge_calibration
from simai_ai.calibration.document import CameraCalibration, build_calibration_doc, content_hash
from simai_ai.calibration.extrinsics.session_bootstrap import rotation_candidates
from simai_ai.calibration.extrinsics.session_tracks import (
    SessionTracks,
    associate_session,
    association_metrics,
    bundle_observations,
    reprojection_metrics,
    split_session_tracks,
)
from simai_ai.calibration.recalibration import intrinsics_init
from simai_ai.geometry.rig import CameraRig
from simai_ai.providers.base import CalibrationProvider
from simai_ai.providers.contract import check_calibration
from simai_ai.tracking.time_model import require_ms

FIXED_LENS = (1284.27, -0.29198, 0.05982)
PROVIDER_VERSION = "simai-session-calibration-0.2.0"


@dataclass(frozen=True)
class SessionDataCriteria:
    """회차 보정을 제안하기 위한 최소 관측 데이터 기준(-343)."""

    min_fit_ticks: int = 30
    min_fit_active_ticks: int = 25
    min_fit_observations: int = 40
    min_fit_3plus_observations: int = 25
    min_camera_3plus_observations: int = 15
    min_holdout_ticks: int = 15
    min_holdout_active_ticks: int = 10


DEFAULT_DATA_CRITERIA = SessionDataCriteria()


class _SessionCalibration(CalibrationProvider):
    def __init__(self, doc):
        self.doc = doc

    def current(self):
        return copy.deepcopy(self.doc)


def session_rig(doc):
    return CameraRig.from_provider(_SessionCalibration(doc))


def _score(frames):
    return sum(len(c.views) ** 2 for f in frames for c in f.clusters
               if len(c.views) >= 2 and not c.identity_uncertain)


def evaluate_calibration(tracks: SessionTracks, doc: Mapping[str, Any], *, min_confidence: float = 0.3) -> dict[str, Any]:
    """보정 문서를 주어진 공통 시각 구간(fit 또는 holdout)에서 연관·삼각측량해 평가 지표를 낸다."""
    rig = session_rig(doc)
    frames = associate_session(tracks, rig)
    obs = bundle_observations(frames)
    metrics = association_metrics(frames)
    metrics["reprojection_error_px"] = reprojection_metrics(frames, rig, min_confidence=min_confidence)
    metrics["observations"] = len(obs)
    metrics["observations_3plus"] = sum(1 for o in obs if len(o.views) >= 3)
    metrics["observations_4view"] = sum(1 for o in obs if len(o.views) >= 4)
    metrics["active_ticks"] = len(frames)
    metrics["total_ticks"] = len(tracks.ticks)
    return metrics


def _pair_pass_means(tracks, doc):
    metrics = association_metrics(associate_session(tracks, session_rig(doc)))
    rates = {}
    for c in doc["cameras"]:
        cid = c["camera_id"]
        values = [v["pass_ratio"] for k, v in metrics["pairs"].items() if cid in k.split("/")]
        rates[cid] = float(np.mean(values)) if values else 0.0
    return rates


def bootstrap_rotations(tracks, calibration):
    current = copy.deepcopy(calibration)
    rates = _pair_pass_means(tracks, current)
    # 통과율이 낮은 카메라부터 시험한다. 앞선 카메라가 복구되면 통과율을 다시 재어 불필요한 복구를 막는다.
    records = []
    for cid in sorted(rates, key=lambda c: (rates[c], c)):
        live_rates = _pair_pass_means(tracks, current)
        if live_rates[cid] >= 0.8 * max(live_rates.values()) and max(live_rates.values()) > 0:
            records.append({"camera_id": cid, "initial_pair_pass_mean": rates[cid],
                            "live_pair_pass_mean": live_rates[cid],
                            "candidates": [], "skipped": "상대적으로 낮은 통과율의 카메라가 아님"})
            continue
        refs = associate_session(tracks, session_rig(current), exclude=(cid,))
        candidates, attempts = rotation_candidates(tracks, refs, current, cid)
        score = _score(associate_session(tracks, session_rig(current)))
        best = None
        for candidate, rec in candidates:
            candidate_score = _score(associate_session(tracks, session_rig(candidate)))
            rec["association_score_before"] = score
            rec["association_score_after"] = candidate_score
            if candidate_score > score and (best is None or candidate_score > best[0]):
                best = (candidate_score, candidate, rec)
            rec["reason"] = "NO_ASSOCIATION_IMPROVEMENT" if candidate_score <= score else "OTHER_CANDIDATE_SELECTED"
        if best is not None:
            current, best[2]["accepted"] = best[1], True
            best[2].pop("reason", None)
        records.append({"camera_id": cid, "initial_pair_pass_mean": rates[cid],
                        "live_pair_pass_mean": live_rates[cid], "candidates": attempts})
    return current, {"method": "TRACK_CANDIDATE_PNP_RANSAC_THEN_FIXED_CENTER_ROTATION", "cameras": records}


def _svd_rotation(H: np.ndarray) -> np.ndarray:
    u, _, vt = np.linalg.svd(H)
    d = float(np.linalg.det(u @ vt))
    return u @ np.diag([1.0, 1.0, 1.0 if d >= 0 else -1.0]) @ vt


def _pose_alignment(previous, cameras, anchors):
    """중심과 방향을 함께 쓰는 닮음 정렬. 모든 카메라의 잔차를 같은 변환으로 잰다."""
    old = {c["camera_id"]: c for c in previous["cameras"]}
    new = {(c.camera_id if isinstance(c, CameraCalibration) else c["camera_id"]):
           (c.to_contract() if isinstance(c, CameraCalibration) else c) for c in cameras}
    r0 = {cid: np.asarray(old[cid]["R"]) for cid in new}
    r = {cid: np.asarray(c["R"]) for cid, c in new.items()}
    c0 = {cid: -r0[cid].T @ np.asarray(old[cid]["t"]) for cid in new}
    centers = {cid: -r[cid].T @ np.asarray(new[cid]["t"]) for cid in new}
    a0, a = np.array([c0[c] for c in anchors]), np.array([centers[c] for c in anchors])
    d0, d = a0 - a0.mean(axis=0), a - a.mean(axis=0)
    norm = float(np.linalg.norm(d0) * np.linalg.norm(d))
    denom = float(np.sum(d ** 2))
    if norm <= 1e-12 or denom <= 1e-12:
        raise ValueError("기준 카메라 중심 간 거리가 없어 축척을 이어받을 수 없다")
    rw = _svd_rotation(d0.T @ d / norm + sum(r0[c].T @ r[c] for c in anchors))
    scale = float(np.sum(d0 * (d @ rw.T)) / denom)
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("직전 보정 기준 닮음 축척이 유효하지 않다")
    translation = a0.mean(axis=0) - scale * rw @ a.mean(axis=0)
    residuals = {cid: float(np.linalg.norm(scale * rw @ centers[cid] + translation - c0[cid]) * 100)
                 for cid in new}
    rotations = {cid: float(np.degrees(Rotation.from_matrix(r[cid] @ rw.T @ r0[cid].T).magnitude()))
                 for cid in new}
    return {"scale": scale, "center_residuals_cm": residuals, "rotation_residuals_deg": rotations,
            "max_cm": max(residuals[c] for c in anchors),
            "rms_cm": float(np.sqrt(np.mean([residuals[c] ** 2 for c in anchors]))),
            "max_rotation_deg": max(rotations[c] for c in anchors)}


def select_anchor_cameras(
    previous: Mapping[str, Any],
    cameras: Sequence[CameraCalibration | Mapping[str, Any]],
    *,
    bootstrap: Mapping[str, Any] | None = None,
    anchor_cameras: Sequence[str] | None = None,
    max_rotation_deg: float = 1.5,
    diagnostics: dict[str, Any] | None = None,
    fixed_center_rotation_cameras: Sequence[str] = (),
) -> tuple[list[str], str, dict[str, str]]:
    """직전 World에 정렬할 기준 카메라를 고른다(기본 auto: PnP 복구 불필요·회전 변화 작은 카메라 최소 2대)."""
    old_by_id = {c["camera_id"]: np.asarray(c["R"], dtype=float) for c in previous["cameras"]}
    new_by_id = {
        (c.camera_id if isinstance(c, CameraCalibration) else c["camera_id"]): np.asarray(
            c.R if isinstance(c, CameraCalibration) else c["R"], dtype=float
        )
        for c in cameras
    }
    known = sorted(set(old_by_id) & set(new_by_id))
    if anchor_cameras is not None:
        chosen = [c.strip() for c in anchor_cameras if c.strip()]
        chosen_unique = list(dict.fromkeys(chosen))
        if len(chosen_unique) < 2 or any(c not in old_by_id for c in chosen_unique):
            raise ValueError(f"기준 카메라는 직전 보정에 있는 서로 다른 카메라 2대 이상이어야 한다: {chosen_unique}")
        excluded = {c: "수동 기준 카메라 목록에서 제외됨" for c in known if c not in chosen_unique}
        return [c for c in chosen_unique if c in new_by_id], "manual", excluded

    bootstrapped = set()
    if bootstrap:
        for cam_rec in bootstrap.get("cameras", ()):
            if any(cand.get("accepted") for cand in cam_rec.get("candidates", ())):
                bootstrapped.add(cam_rec["camera_id"])
    excluded: dict[str, str] = {}
    pool = []
    for cid in known:
        if cid in bootstrapped:
            excluded[cid] = "PnP 회전 초기 복구가 적용됨"
        else:
            pool.append(cid)
    diagnostics = diagnostics if diagnostics is not None else {}
    # 먼저 전체 정렬을 기록한다. 과거 fast_rotation은 중심을 고치지 않았으므로 기준에서 뺀다.
    try:
        if len(known) >= 2:
            diagnostics["all_cameras_alignment"] = _pose_alignment(previous, cameras, known)
    except ValueError as exc:
        diagnostics["all_cameras_alignment"] = {"failure_reason": str(exc)}
    for cid in fixed_center_rotation_cameras:
        if cid in pool:
            pool.remove(cid)
            excluded[cid] = "직전 빠른 회전 보정에서 중심은 고정되고 회전만 변경됨"
    diagnostics["leave_one_out"] = []
    while len(pool) >= 3:
        candidates = {}
        for cid in pool:
            try:
                candidates[cid] = _pose_alignment(previous, cameras, [c for c in pool if c != cid])
            except ValueError:
                continue
        diagnostics["leave_one_out"].append({"pool": list(pool), "candidates": candidates})
        if not candidates:
            break
        worst = min(candidates, key=lambda c: (candidates[c]["max_cm"], candidates[c]["rms_cm"],
                                               candidates[c]["max_rotation_deg"]))
        fit = candidates[worst]
        center_outlier = (fit["center_residuals_cm"][worst] > 3.5
                          and fit["center_residuals_cm"][worst] > 1.8 * fit["max_cm"])
        rotation_outlier = (fit["rotation_residuals_deg"][worst] > max_rotation_deg
                            and fit["max_rotation_deg"] <= max_rotation_deg)
        if not center_outlier and not rotation_outlier:
            break
        excluded[worst] = (f"LOO 이탈: 중심 {fit['center_residuals_cm'][worst]:.3f} cm, "
                           f"회전 {fit['rotation_residuals_deg'][worst]:.3f}°, "
                           f"남은 최대 중심 잔차 {fit['max_cm']:.3f} cm")
        pool.remove(worst)
    while len(pool) >= 2:
        R_w = _svd_rotation(sum(old_by_id[c].T @ new_by_id[c] for c in pool))
        residuals = {
            c: float(np.degrees(Rotation.from_matrix((new_by_id[c] @ R_w.T) @ old_by_id[c].T).magnitude()))
            for c in pool
        }
        worst = max(residuals, key=residuals.get)
        if residuals[worst] <= max_rotation_deg:
            break
        if len(pool) == 2:
            for c, val in residuals.items():
                if val > max_rotation_deg:
                    excluded[c] = f"공통 World 회전 제거 뒤 회전 변화 {val:.2f}° > {max_rotation_deg:.2f}°"
            pool = [c for c in pool if c not in excluded]
            break
        excluded[worst] = f"공통 World 회전 제거 뒤 회전 변화 {residuals[worst]:.2f}° > {max_rotation_deg:.2f}°"
        pool.remove(worst)
    return pool, "auto", excluded


def align_world_to_previous(
    previous: Mapping[str, Any],
    cameras: Sequence[CameraCalibration | Mapping[str, Any]],
    measured_camera_heights: Mapping[str, float] | None = None,
    *,
    bootstrap: Mapping[str, Any] | None = None,
    anchor_cameras: Sequence[str] | None = None,
    max_anchor_rotation_deg: float = 1.5,
    apply_scale: bool = True,
    fixed_center_rotation_cameras: Sequence[str] = (),
) -> tuple[tuple[CameraCalibration, ...], dict[str, Any]]:
    """기준 카메라들의 중심과 방향을 써서 새 보정의 World를 직전 보정의 World에 닮음 변환으로 맞춘다."""
    cam_objs = tuple(
        c if isinstance(c, CameraCalibration) else CameraCalibration(
            camera_id=c["camera_id"],
            image_size=tuple(c["image_size"]),
            K=c["K"],
            R=c["R"],
            t=c["t"],
            distortion_model=c["distortion"]["model"],
            distortion_coeffs=c["distortion"]["coeffs"],
            reprojection_median_px=(c.get("reprojection_error_px") or {}).get("median"),
            reprojection_p95_px=(c.get("reprojection_error_px") or {}).get("p95"),
        )
        for c in cameras
    )
    if previous.get("state") == "UNCALIBRATED":
        return cam_objs, {"usable": False, "anchor_cameras": [],
                          "failure_reason": "직전 미터 보정이 없어 처음 설치 보정이 필요하다"}
    diagnostics: dict[str, Any] = {}
    anchors, mode, excluded = select_anchor_cameras(
        previous, cam_objs, bootstrap=bootstrap, anchor_cameras=anchor_cameras, max_rotation_deg=max_anchor_rotation_deg,
        diagnostics=diagnostics, fixed_center_rotation_cameras=fixed_center_rotation_cameras,
    )
    if len(anchors) < 2:
        return cam_objs, {
            **diagnostics,
            "usable": False,
            "selection_mode": mode,
            "anchor_cameras": anchors,
            "excluded_cameras": excluded,
            "failure_reason": f"World 정렬 기준 카메라 부족({len(anchors)}대 < 2대)",
        }
    try:
        _pose_alignment(previous, cam_objs, anchors)
    except ValueError as exc:
        return cam_objs, {**diagnostics, "usable": False, "anchor_cameras": anchors,
                          "excluded_cameras": excluded, "failure_reason": str(exc)}
    old_map = {c["camera_id"]: c for c in previous["cameras"]}
    new_map = {c.camera_id: c for c in cam_objs}
    R0_map = {cid: np.asarray(old_map[cid]["R"], dtype=float) for cid in new_map}
    C0_map = {cid: -R0_map[cid].T @ np.asarray(old_map[cid]["t"], dtype=float) for cid in new_map}
    R_map = {cid: np.asarray(new_map[cid].R, dtype=float) for cid in new_map}
    C_map = {cid: -R_map[cid].T @ np.asarray(new_map[cid].t, dtype=float) for cid in new_map}

    C0_anc = np.array([C0_map[cid] for cid in anchors], dtype=float)
    C_anc = np.array([C_map[cid] for cid in anchors], dtype=float)
    C0_mean = C0_anc.mean(axis=0)
    C_mean = C_anc.mean(axis=0)
    dC0 = C0_anc - C0_mean
    dC = C_anc - C_mean
    norm_prod = float(np.linalg.norm(dC0) * np.linalg.norm(dC))
    H_centers = (dC0.T @ dC) / norm_prod if norm_prod > 1e-12 else np.zeros((3, 3))
    H_rots = sum(R0_map[cid].T @ R_map[cid] for cid in anchors)
    R_w = _svd_rotation(H_centers + H_rots)

    denom = float(np.sum(dC ** 2))
    s_sim = float(np.sum(dC0 * (dC @ R_w.T)) / denom) if denom > 1e-12 else 1.0
    if s_sim <= 0:
        s_sim = 1.0
    t_w_sim = C0_mean - s_sim * (R_w @ C_mean)
    t_w_rigid = C0_mean - (R_w @ C_mean)
    s_used = s_sim if apply_scale else 1.0
    t_w = t_w_sim if apply_scale else t_w_rigid

    aligned: list[CameraCalibration] = []
    C_sim_map: dict[str, np.ndarray] = {}
    C_rig_map: dict[str, np.ndarray] = {}
    R_aligned_map: dict[str, np.ndarray] = {}
    for cam in cam_objs:
        cid = cam.camera_id
        R_new = R_map[cid] @ R_w.T
        C_sim = s_sim * (R_w @ C_map[cid]) + t_w_sim
        C_rig = (R_w @ C_map[cid]) + t_w_rigid
        C_used = C_sim if apply_scale else C_rig
        t_new = -R_new @ C_used
        C_sim_map[cid] = C_sim
        C_rig_map[cid] = C_rig
        R_aligned_map[cid] = R_new
        aligned.append(replace(cam, R=R_new.tolist(), t=t_new.tolist()))

    C_used_map = C_sim_map if apply_scale else C_rig_map
    anchor_res_cm = {cid: round(float(np.linalg.norm(C_used_map[cid] - C0_map[cid]) * 100), 3) for cid in anchors}
    anchor_res_sim_cm = {cid: round(float(np.linalg.norm(C_sim_map[cid] - C0_map[cid]) * 100), 3) for cid in anchors}
    anchor_res_rig_cm = {cid: round(float(np.linalg.norm(C_rig_map[cid] - C0_map[cid]) * 100), 3) for cid in anchors}
    anchor_rot_deg = {
        cid: round(float(np.degrees(Rotation.from_matrix(R_aligned_map[cid] @ R0_map[cid].T).magnitude())), 4)
        for cid in anchors
    }
    rot_obj = Rotation.from_matrix(R_w)
    rot_deg = float(np.degrees(rot_obj.magnitude()))
    rotvec_deg = np.degrees(rot_obj.as_rotvec())

    height_record = None
    height_residuals = None
    if measured_camera_heights:
        heights_after = {cid: float(C_used_map[cid][2]) for cid in C_used_map}
        scale_after, h_rec = checks.camera_height_scale(
            heights_after, measured_camera_heights, min_cameras=2, max_spread=0.03, floor_known=True
        )
        height_record = {**dict(h_rec), "scale_after": None if scale_after is None else round(float(scale_after), 6)}
        height_residuals = {
            "before_alignment_m": {
                cid: round(float(C_map[cid][2] - measured_camera_heights[cid]), 4)
                for cid in sorted(C_map) if cid in measured_camera_heights
            },
            "after_similarity_m": {
                cid: round(float(C_sim_map[cid][2] - measured_camera_heights[cid]), 4)
                for cid in sorted(C_sim_map) if cid in measured_camera_heights
            },
            "after_rigid_m": {
                cid: round(float(C_rig_map[cid][2] - measured_camera_heights[cid]), 4)
                for cid in sorted(C_rig_map) if cid in measured_camera_heights
            },
        }
        # 검산 배율로 World를 바꾸지 않는다. 카메라 간 3% 또는 절대 높이 10cm 초과는 거부한다.
        height_max_error = max((abs(C_used_map[c][2] - measured_camera_heights[c])
                                for c in C_used_map if c in measured_camera_heights), default=0.)
        height_record["max_absolute_residual_m"] = round(float(height_max_error), 6)
        if scale_after is None or height_max_error > 0.10:
            return tuple(aligned), {
                **diagnostics,
                "usable": False,
                "mode": "similarity" if apply_scale else "rigid",
                "selection_mode": mode,
                "anchor_cameras": anchors,
                "excluded_cameras": excluded,
                "rotation_deg": round(rot_deg, 4),
                "translation_cm": round(float(np.linalg.norm(t_w) * 100), 3),
                "scale": round(s_used, 6),
                "similarity_scale": round(s_sim, 6),
                "height_scale_diff_ratio": round(s_sim - 1.0, 6),
                "height_scale_check_after": height_record,
                "height_residuals_m": height_residuals,
                "anchor_center_residuals_cm": anchor_res_cm,
                "failure_reason": f"World 정렬 뒤 실측 카메라 높이 검산 실패: {h_rec.get('rejected') or '절대 높이 잔차 > 0.10 m'}",
            }

    return tuple(aligned), {
        **diagnostics,
        "usable": True,
        "scale_basis": "PREVIOUS_CALIBRATION",
        "strict_two_anchor_limits_applied": len(anchors) == 2,
        "scale_change_ratio": max(
            abs(float(np.linalg.norm(C_used_map[a] - C_used_map[b]) / np.linalg.norm(C0_map[a] - C0_map[b])) - 1)
            for i, a in enumerate(anchors) for b in anchors[i + 1:]
            if np.linalg.norm(C0_map[a] - C0_map[b]) > 1e-12
        ),
        "mode": "similarity" if apply_scale else "rigid",
        "selection_mode": mode,
        "anchor_cameras": anchors,
        "excluded_cameras": excluded,
        "rotation_deg": round(rot_deg, 4),
        "rotation_axis_angle_deg": [round(float(v), 4) for v in rotvec_deg],
        "rotation_matrix": [[round(float(v), 8) for v in row] for row in R_w],
        "translation_cm": round(float(np.linalg.norm(t_w) * 100), 3),
        "translation_vector_cm": [round(float(v) * 100, 3) for v in t_w],
        "centroid_shift_cm": round(float(np.linalg.norm(C0_mean - C_mean) * 100), 3),
        "centroid_shift_vector_cm": [round(float(v) * 100, 3) for v in (C0_mean - C_mean)],
        "scale": round(s_used, 6),
        "similarity_scale": round(s_sim, 6),
        "height_scale_diff_ratio": round(s_sim - 1.0, 6),
        "height_scale_check_after": height_record,
        "height_residuals_m": height_residuals,
        "anchor_center_residuals_cm": anchor_res_cm,
        "anchor_center_residual_mean_cm": round(float(np.mean(list(anchor_res_cm.values()))), 3),
        "anchor_center_residual_max_cm": round(float(np.max(list(anchor_res_cm.values()))), 3),
        "anchor_center_residuals_similarity_cm": anchor_res_sim_cm,
        "anchor_center_residuals_rigid_cm": anchor_res_rig_cm,
        "anchor_rotation_residuals_deg": anchor_rot_deg,
    }


def check_data_sufficiency(
    fit_tracks: SessionTracks,
    holdout_tracks: SessionTracks,
    observations: Sequence[ Any],
    camera_ids: Sequence[str],
    criteria: SessionDataCriteria = DEFAULT_DATA_CRITERIA,
) -> dict[str, Any]:
    """보정용(fit) 및 독립 검증용(holdout) 관측량이 최소 기준을 만족하는지 점검한다."""
    fit_ticks = len(fit_tracks.ticks)
    fit_active = sum(1 for _, vs in fit_tracks.ticks if len({v.camera_id for v in vs}) >= 2)
    holdout_ticks = len(holdout_tracks.ticks)
    holdout_active = sum(1 for _, vs in holdout_tracks.ticks if len({v.camera_id for v in vs}) >= 2)
    n_obs = len(observations)
    n_3plus = sum(1 for o in observations if len(o.views) >= 3)
    n_4view = sum(1 for o in observations if len(o.views) >= 4)
    per_cam_3plus = {cid: sum(1 for o in observations if len(o.views) >= 3 and cid in o.views) for cid in sorted(camera_ids)}
    reasons: list[str] = []
    if fit_ticks < criteria.min_fit_ticks:
        reasons.append(f"fit 공통 시각 {fit_ticks}개 < {criteria.min_fit_ticks}개")
    if fit_active < criteria.min_fit_active_ticks:
        reasons.append(f"fit 다중 카메라 활성 시각 {fit_active}개 < {criteria.min_fit_active_ticks}개")
    if holdout_ticks < criteria.min_holdout_ticks:
        reasons.append(f"holdout 공통 시각 {holdout_ticks}개 < {criteria.min_holdout_ticks}개")
    if holdout_active < criteria.min_holdout_active_ticks:
        reasons.append(f"holdout 다중 카메라 활성 시각 {holdout_active}개 < {criteria.min_holdout_active_ticks}개")
    if n_obs < criteria.min_fit_observations:
        reasons.append(f"fit 다중인 관측 {n_obs}개 < {criteria.min_fit_observations}개")
    if n_3plus < criteria.min_fit_3plus_observations:
        reasons.append(f"fit 3대 이상 동시 관측 {n_3plus}개 < {criteria.min_fit_3plus_observations}개")
    low_cams = {cid: cnt for cid, cnt in per_cam_3plus.items() if cnt < criteria.min_camera_3plus_observations}
    if low_cams:
        reasons.append(f"카메라별 3대 이상 관측 부족(< {criteria.min_camera_3plus_observations}개): {low_cams}")
    return {
        "usable": not reasons,
        "reasons": reasons,
        "counts": {
            "fit_ticks": fit_ticks,
            "fit_active_ticks": fit_active,
            "holdout_ticks": holdout_ticks,
            "holdout_active_ticks": holdout_active,
            "fit_observations": n_obs,
            "fit_3plus_observations": n_3plus,
            "fit_4view_observations": n_4view,
            "camera_3plus_observations": per_cam_3plus,
        },
        "criteria": {
            "min_fit_ticks": criteria.min_fit_ticks,
            "min_fit_active_ticks": criteria.min_fit_active_ticks,
            "min_holdout_ticks": criteria.min_holdout_ticks,
            "min_holdout_active_ticks": criteria.min_holdout_active_ticks,
            "min_fit_observations": criteria.min_fit_observations,
            "min_fit_3plus_observations": criteria.min_fit_3plus_observations,
            "min_camera_3plus_observations": criteria.min_camera_3plus_observations,
        },
    }


@dataclass(frozen=True)
class SessionResult:
    calibration: Mapping | None
    report: Mapping


def adjust_session(
    tracks: SessionTracks,
    previous: Mapping[str, Any],
    *,
    measured_camera_heights: Mapping[str, float] | None = None,
    input_provenance: Mapping[str, Any],
    created_at: int,
    rounds: int = 3,
    fixed_lens: tuple[float, float, float] = FIXED_LENS,
    bundle_config: BundleConfig | None = None,
    holdout_tracks: SessionTracks | None = None,
    anchor_cameras: Sequence[str] | None = None,
    data_criteria: SessionDataCriteria = DEFAULT_DATA_CRITERIA,
    apply_world_scale: bool = True,
    progress: Callable[[str], None] | None = None,
    fixed_center_rotation_cameras: Sequence[str] = (),
) -> SessionResult:
    require_ms("created_at", created_at)
    measured_camera_heights = measured_camera_heights or {}
    if rounds not in (2, 3):
        raise ValueError("session 연관/조정 라운드는 2 또는 3이다")
    problems = check_calibration(previous)
    if problems:
        raise ValueError(f"직전 보정이 계약에 맞지 않는다: {problems}")
    if previous.get("content_hash") != content_hash(previous):
        raise ValueError("직전 보정 content_hash가 내용과 다르다")
    config = bundle_config or BundleConfig(lens_mode="fixed", fixed_lens=fixed_lens)
    if config.lens_mode != "fixed" or config.fixed_lens != fixed_lens:
        raise ValueError("session은 공유 고정 렌즈를 쓴다")
    config = replace(config, floor_contacts_for_gauge=False)
    if anchor_cameras is not None:
        select_anchor_cameras(previous, previous["cameras"], anchor_cameras=anchor_cameras)

    if holdout_tracks is None:
        fit_tracks, holdout_tracks, split_record = split_session_tracks(tracks)
    else:
        fit_tracks = tracks
        fit_set = {t for t, _ in fit_tracks.ticks}
        hold_set = {t for t, _ in holdout_tracks.ticks}
        overlap = len(fit_set & hold_set)
        if overlap > 0:
            raise ValueError(f"fit과 holdout 구간의 공통 시각이 {overlap}개 겹친다")
        split_record = {
            "method": "explicit_holdout",
            "fit_ticks": len(fit_tracks.ticks),
            "holdout_ticks": len(holdout_tracks.ticks),
            "fit_active_ticks": sum(1 for _, vs in fit_tracks.ticks if len({v.camera_id for v in vs}) >= 2),
            "holdout_active_ticks": sum(1 for _, vs in holdout_tracks.ticks if len({v.camera_id for v in vs}) >= 2),
            "overlap_ticks": 0,
        }

    current = copy.deepcopy(previous)
    for c in current["cameras"]:
        c["K"][0][0] = c["K"][1][1] = fixed_lens[0]
        c["distortion"] = {"model": "OPENCV_PINHOLE", "coeffs": [*fixed_lens[1:], 0., 0., 0.]}
        c.pop("reprojection_error_px", None)
    previous_fixed = copy.deepcopy(current)

    fit_before = evaluate_calibration(fit_tracks, previous_fixed, min_confidence=config.min_keypoint_confidence)
    holdout_before = evaluate_calibration(holdout_tracks, previous_fixed, min_confidence=config.min_keypoint_confidence)
    if progress:
        progress(
            f"구간 분리({split_record['method']}): fit {len(fit_tracks.ticks)}개 시각, "
            f"holdout {len(holdout_tracks.ticks)}개 시각. 낮은 통과율 카메라의 PnP 후보를 시험한다"
        )
    current, bootstrap = bootstrap_rotations(fit_tracks, current)

    expected_camera_ids = [c["camera_id"] for c in previous["cameras"]]
    initial_frames = associate_session(fit_tracks, session_rig(current))
    initial_obs = bundle_observations(initial_frames)
    sufficiency = check_data_sufficiency(fit_tracks, holdout_tracks, initial_obs, expected_camera_ids, data_criteria)

    report: dict[str, Any] = {
        "format": "simai.session_calibration",
        "format_version": "0.2.0",
        "scale_basis": "PREVIOUS_CALIBRATION",
        "created_at": created_at,
        "previous_version": previous["calibration_version"],
        "input_provenance": dict(input_provenance),
        "tracking": dict(tracks.record),
        "split": split_record,
        "data_sufficiency": sufficiency,
        "lens": {"mode": "fixed", "values": list(fixed_lens)},
        "before": fit_before,
        "bootstrap": bootstrap,
        "rounds": [],
        "world_alignment": None,
        "calibration": None,
    }
    if not sufficiency["usable"]:
        report["failure_reason"] = "최소 데이터 기준 미달: " + "; ".join(sufficiency["reasons"])
        report["after"] = fit_before
        report["fit"] = {"before": fit_before, "after": fit_before}
        report["holdout"] = {"before": holdout_before, "after": holdout_before}
        report["camera_changes"] = camera_changes(previous, current, measured_camera_heights)
        report["candidate_cameras"] = current["cameras"]
        return SessionResult(None, report)

    result = None
    unaligned_cameras: list[dict[str, Any]] = []
    aligned_cameras: tuple[CameraCalibration, ...] = ()
    for n in range(rounds):
        if progress:
            progress(f"교대 조정 {n + 1}/{rounds}: fit 구간 원본 트랙을 다시 연관한다")
        frames = initial_frames if n == 0 else associate_session(fit_tracks, session_rig(current))
        observations = initial_obs if n == 0 else bundle_observations(frames)
        entry = {"round": n + 1, "association": association_metrics(frames), "observations": len(observations)}
        report["rounds"].append(entry)
        if not observations:
            report["failure_reason"] = "다중인 대응 부족"
            break
        result = adjust_human_bundle(
            observations, intrinsics_init(current), {c["camera_id"]: (c["R"], c["t"]) for c in current["cameras"]},
            measured_camera_heights=None, input_provenance=input_provenance,
            created_at=created_at, config=config,
        )
        entry["bundle"] = dict(result.record)
        entry["status"], entry["flags"] = result.status, list(result.flags)
        # 바닥·단안 높이 검산의 미상/불일치는 상대 포즈의 수렴 실패가 아니다.
        check_flags = {FLAG_CONTACTS_COLLINEAR, FLAG_FEW_CONTACTS, FLAG_FLOOR_PLANE_UNKNOWN,
                       FLAG_HEIGHT_CHECK_MISMATCH, FLAG_HEIGHT_CHECK_UNKNOWN}
        blocking_flags = tuple(f for f in result.flags if f.split(":")[0] not in check_flags)
        entry["blocking_flags"] = list(blocking_flags)
        if result.status == "UNCERTAIN" and not blocking_flags:
            result = replace(result, status="CONVERGED")
        entry["status"] = result.status
        entry["floor_check"] = result.record.get("floor")
        if progress:
            progress(f"라운드 {n + 1}: {len(observations)}개 사람/시각, {result.status}, {list(result.flags)}")
        if not result.cameras:
            report["failure_reason"] = f"번들 조정 {result.status}"
            break
        unaligned_cameras = [c.to_contract() for c in result.cameras]
        aligned_cameras, world_alignment = align_world_to_previous(
            previous,
            result.cameras,
            measured_camera_heights,
            bootstrap=bootstrap,
            anchor_cameras=anchor_cameras,
            apply_scale=apply_world_scale,
            fixed_center_rotation_cameras=fixed_center_rotation_cameras,
        )
        entry["world_alignment"] = world_alignment
        report["world_alignment"] = world_alignment
        if world_alignment.get("usable"):
            current["cameras"] = [c.to_contract() for c in aligned_cameras]
        else:
            current["cameras"] = unaligned_cameras
            report.setdefault("failure_reason", world_alignment.get("failure_reason", "World 정렬 실패"))
        # UNCERTAIN이어도 다음 연관의 후보 초기값으로만 쓴다. 최종 제안은 여전히 거른다.

    report["camera_changes_before_world_alignment"] = (
        camera_changes(previous, {"cameras": unaligned_cameras}, measured_camera_heights)
        if unaligned_cameras else camera_changes(previous, current, measured_camera_heights)
    )
    if report.get("world_alignment") and report["world_alignment"].get("usable") and progress:
        wa = report["world_alignment"]
        progress(
            f"World 정렬({wa['mode']}, 기준 {wa['anchor_cameras']}): "
            f"회전 {wa['rotation_deg']}°, 이동 {wa['translation_cm']} cm, 축척 {wa['scale']}"
        )

    fit_after = evaluate_calibration(fit_tracks, current, min_confidence=config.min_keypoint_confidence)
    holdout_after = evaluate_calibration(holdout_tracks, current, min_confidence=config.min_keypoint_confidence)
    report["after"] = fit_after
    report["fit"] = {"before": fit_before, "after": fit_after}
    report["holdout"] = {"before": holdout_before, "after": holdout_after}
    report["camera_changes"] = camera_changes(previous, current, measured_camera_heights)
    report["candidate_cameras"] = current["cameras"]

    if result is None or result.status != "CONVERGED":
        report.setdefault("failure_reason", "최종 번들 조정 불확실: " + ", ".join(result.flags if result else ()))
        return SessionResult(None, report)
    if {c.camera_id for c in result.cameras} != set(expected_camera_ids):
        report["failure_reason"] = "설치 카메라 전체의 보정 관측이 없다"
        return SessionResult(None, report)
    if not report.get("world_alignment") or not report["world_alignment"].get("usable"):
        report.setdefault("failure_reason", "직전 World 정렬에 실패했다")
        return SessionResult(None, report)
    cameras = [c.to_contract() for c in aligned_cameras]
    evidence = result.evidence()
    # 높이는 검산만 한다. 문서에는 축척을 실제로 정한 직전 근거를 이어받는다.
    evidence = replace(evidence, scale_estimates=())
    judgement = judge_calibration(result.provider_source, cameras, evidence,
                                  expected_camera_ids=expected_camera_ids)
    stamp = dt.datetime.fromtimestamp(created_at / 1000, dt.UTC).strftime("%Y%m%d-%H%M%S")
    doc = build_calibration_doc(
        calibration_version=f"cal-session-{stamp}-{created_at % 1000:03d}z",
        provider_source=result.provider_source, provider_version=PROVIDER_VERSION,
        created_at=created_at, cameras=aligned_cameras, judgement=judgement,
        fallback_scale_basis=previous["scale_basis"],
        site_id=previous["coordinate_frame"].get("site_id"), world_axes_source=previous.get("world_axes_source"),
    )
    if doc["state"] != "PROVISIONAL":
        raise ValueError("독립 검산 없는 session 보정은 PROVISIONAL이어야 한다")
    errors = check_calibration(doc)
    if errors:
        raise ValueError(f"새 보정 계약 검증 실패: {errors}")
    report["calibration"] = {"version": doc["calibration_version"], "state": doc["state"],
                             "content_hash": doc["content_hash"], "schema_errors": errors}
    return SessionResult(doc, report)


def camera_changes(previous, current, heights=None):
    heights = heights or {}
    old = {c["camera_id"]: c for c in previous["cameras"]}
    changes = {}
    for c in current["cameras"]:
        cid = c["camera_id"]
        a, b = np.asarray(old[cid]["R"]), np.asarray(c["R"])
        C0, C = -a.T @ np.asarray(old[cid]["t"]), -b.T @ np.asarray(c["t"])
        changes[cid] = {"rotation_deg": float(np.degrees(Rotation.from_matrix(b @ a.T).magnitude())),
                        "position_cm": float(np.linalg.norm(C - C0) * 100),
                        "position_delta_cm": ((C - C0) * 100).tolist(), "height_m": float(C[2]),
                        "height_residual_m": float(C[2] - heights[cid]) if cid in heights else None,
                        "reprojection_error_px": c.get("reprojection_error_px")}
    return changes
