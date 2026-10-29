"""회차 녹화의 다중인 연관과 고정 렌즈 번들 조정을 교대로 수행한다(-331)."""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np
from scipy.spatial.transform import Rotation

from simai_ai.calibration.bundle import BundleConfig, adjust_human_bundle
from simai_ai.calibration.criteria import judge_calibration
from simai_ai.calibration.document import build_calibration_doc, content_hash
from simai_ai.calibration.extrinsics.session_bootstrap import rotation_candidates
from simai_ai.calibration.extrinsics.session_tracks import (
    associate_session, association_metrics, bundle_observations,
)
from simai_ai.calibration.recalibration import intrinsics_init
from simai_ai.geometry.rig import CameraRig
from simai_ai.providers.base import CalibrationProvider
from simai_ai.providers.contract import check_calibration
from simai_ai.tracking.time_model import require_ms

FIXED_LENS = (1284.27, -0.29198, 0.05982)


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


def bootstrap_rotations(tracks, calibration):
    current = copy.deepcopy(calibration)
    before = associate_session(tracks, session_rig(current))
    baseline = association_metrics(before)
    # 통과율이 낮은 카메라부터 시험한다. 대상 카메라를 빼고 기준 궤적을 다시 연관한다.
    rates = {}
    for c in current["cameras"]:
        cid = c["camera_id"]
        values = [v["pass_ratio"] for k, v in baseline["pairs"].items() if cid in k.split("/")]
        rates[cid] = float(np.mean(values)) if values else 0.0
    records = []
    for cid in sorted(rates, key=lambda c: (rates[c], c)):
        if rates[cid] >= 0.8 * max(rates.values()) and max(rates.values()) > 0:
            records.append({"camera_id": cid, "initial_pair_pass_mean": rates[cid],
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
        records.append({"camera_id": cid, "initial_pair_pass_mean": rates[cid], "candidates": attempts})
    return current, {"method": "TRACK_CANDIDATE_PNP_RANSAC_THEN_FIXED_CENTER_ROTATION", "cameras": records}


@dataclass(frozen=True)
class SessionResult:
    calibration: Mapping | None
    report: Mapping


def adjust_session(tracks, previous, *, measured_camera_heights, input_provenance,
                   created_at, rounds=3, fixed_lens=FIXED_LENS, bundle_config=None,
                   progress: Callable[[str], None] | None = None):
    require_ms("created_at", created_at)
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
    current = copy.deepcopy(previous)
    for c in current["cameras"]:
        c["K"][0][0] = c["K"][1][1] = fixed_lens[0]
        c["distortion"] = {"model": "OPENCV_PINHOLE", "coeffs": [*fixed_lens[1:], 0., 0., 0.]}
        c.pop("reprojection_error_px", None)
    before = association_metrics(associate_session(tracks, session_rig(current)))
    if progress:
        progress("직전 보정으로 연관을 측정했다. 낮은 통과율 카메라의 PnP 후보를 시험한다")
    current, bootstrap = bootstrap_rotations(tracks, current)
    report = {"format": "simai.session_calibration", "format_version": "0.1.0",
              "created_at": created_at, "previous_version": previous["calibration_version"],
              "input_provenance": dict(input_provenance), "tracking": dict(tracks.record),
              "lens": {"mode": "fixed", "values": list(fixed_lens)},
              "before": before, "bootstrap": bootstrap, "rounds": [], "calibration": None}
    result = None
    for n in range(rounds):
        if progress:
            progress(f"교대 조정 {n + 1}/{rounds}: 원본 트랙 전체를 다시 연관한다")
        frames = associate_session(tracks, session_rig(current))
        observations = bundle_observations(frames)
        entry = {"round": n + 1, "association": association_metrics(frames), "observations": len(observations)}
        report["rounds"].append(entry)
        if not observations:
            report["failure_reason"] = "다중인 대응 부족"
            break
        result = adjust_human_bundle(
            observations, intrinsics_init(current), {c["camera_id"]: (c["R"], c["t"]) for c in current["cameras"]},
            measured_camera_heights=measured_camera_heights, input_provenance=input_provenance,
            created_at=created_at, config=config,
        )
        entry["bundle"] = dict(result.record)
        entry["status"], entry["flags"] = result.status, list(result.flags)
        if progress:
            progress(f"라운드 {n + 1}: {len(observations)}개 사람/시각, {result.status}, {list(result.flags)}")
        if not result.cameras:
            report["failure_reason"] = f"번들 조정 {result.status}"
            break
        current["cameras"] = [c.to_contract() for c in result.cameras]
        # UNCERTAIN이어도 다음 연관의 후보 초기값으로만 쓴다. 최종 제안은 여전히 거른다.
    report["after"] = association_metrics(associate_session(tracks, session_rig(current)))
    report["camera_changes"] = camera_changes(previous, current, measured_camera_heights)
    report["candidate_cameras"] = current["cameras"]
    if result is None or result.status != "CONVERGED":
        report.setdefault("failure_reason", "최종 번들 조정 불확실: " + ", ".join(result.flags if result else ()))
        return SessionResult(None, report)
    if {c.camera_id for c in result.cameras} != {c["camera_id"] for c in previous["cameras"]}:
        report["failure_reason"] = "설치 카메라 전체의 보정 관측이 없다"
        return SessionResult(None, report)
    scale = result.record.get("scale", {}).get("camera_heights") or {}
    if not scale.get("applied"):
        report["failure_reason"] = "실측 카메라 높이 축척을 적용하지 못했다"
        return SessionResult(None, report)
    cameras = [c.to_contract() for c in result.cameras]
    judgement = judge_calibration(result.provider_source, cameras, result.evidence(),
                                  expected_camera_ids=[c["camera_id"] for c in previous["cameras"]])
    stamp = dt.datetime.fromtimestamp(created_at / 1000, dt.UTC).strftime("%Y%m%d-%H%M%S")
    doc = build_calibration_doc(
        calibration_version=f"cal-session-{stamp}-{created_at % 1000:03d}z",
        provider_source=result.provider_source, provider_version="simai-session-calibration-0.1.0",
        created_at=created_at, cameras=result.cameras, judgement=judgement,
        fallback_scale_basis=result.fallback_scale_basis,
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


def camera_changes(previous, current, heights):
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
