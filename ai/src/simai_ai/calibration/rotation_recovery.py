"""배경만 쓰는 중심 고정 회전 후보. World→camera, 미터·라디안, 시각은 호출자가 준다."""

from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping

import cv2
import numpy as np

from simai_ai.calibration.document import content_hash

METHOD = "BACKGROUND_FIXED_CENTER_ROTATION"


@dataclass(frozen=True)
class RotationConfig:
    min_angle_rad: float = math.radians(0.5)
    max_angle_rad: float = math.radians(15)
    max_residual_px: float = 3.0
    min_singular_ratio: float = 0.98
    min_inlier_ratio: float = 0.6
    min_matches: int = 40
    min_coverage: float = 0.12
    max_features: int = 1600
    max_image_side: int = 960
    max_pair_p95_px: float = 5.0
    min_pair_gain: float = 0.1
    max_pair_worsening_px: float = 0.5
    confirmation_tolerance_rad: float = math.radians(0.5)

    def __post_init__(self):
        for key, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"rotation_recovery.{key}: 유한한 양수 필요")
        for key in ("min_matches", "max_features", "max_image_side"):
            if not isinstance(getattr(self, key), int):
                raise ValueError(f"rotation_recovery.{key}: 정수 필요")
        if self.min_matches < 20 or self.max_features < self.min_matches or self.max_image_side < 64:
            raise ValueError("rotation_recovery: 특징점/영상 크기 부족")
        if not self.min_angle_rad < self.max_angle_rad < math.pi:
            raise ValueError("rotation_recovery: 회전각 범위 오류")
        if any(getattr(self, k) > 1 for k in ("min_singular_ratio", "min_inlier_ratio", "min_coverage", "min_pair_gain")):
            raise ValueError("rotation_recovery: 비율은 1 이하")


@dataclass(frozen=True)
class Features:
    points: np.ndarray  # 왜곡 제거한 원본 해상도의 픽셀 좌표
    descriptors: np.ndarray | None
    size: tuple[int, int]


def extract_features(image, mask, camera, config: RotationConfig) -> Features:
    if tuple(image.shape[1::-1]) != tuple(camera["image_size"]):
        raise ValueError("STREAM_CHANGED: 배경과 내부 파라미터의 해상도가 다름")
    h, w = image.shape[:2]
    scale = min(1., config.max_image_side / max(w, h))
    size = (round(w * scale), round(h * scale))
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    gray = cv2.resize(gray, size)
    valid = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST)
    # 마스크 경계의 descriptor patch도 관측된 정적 배경만 쓴다.
    valid = cv2.erode(valid, np.ones((9, 9), np.uint8))
    keypoints, descriptors = cv2.SIFT_create(nfeatures=config.max_features).detectAndCompute(gray, valid)
    points = np.array([k.pt for k in keypoints], dtype=np.float64).reshape(-1, 2)
    points /= np.array([size[0] / w, size[1] / h])
    distortion = camera["distortion"]
    if distortion["model"] not in ("NONE", "OPENCV"):
        raise ValueError("지원하지 않는 렌즈 왜곡 모델")
    if len(points):
        K = np.asarray(camera["K"], dtype=float)
        coeffs = np.asarray(distortion["coeffs"], dtype=float)
        points = cv2.undistortPoints(points[:, None], K, coeffs if coeffs.size else None, P=K)[:, 0]
    return Features(points, descriptors, (w, h))


def match_features(a: Features, b: Features):
    if a.descriptors is None or b.descriptors is None or min(len(a.points), len(b.points)) < 2:
        return np.empty((0, 2)), np.empty((0, 2))
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    forward = matcher.knnMatch(a.descriptors, b.descriptors, k=2)
    reverse = matcher.knnMatch(b.descriptors, a.descriptors, k=2)
    backwards = {m.queryIdx: m.trainIdx for pair in reverse if len(pair) == 2
                 for m, n in [pair] if m.distance < .75 * n.distance}
    good = [m for pair in forward if len(pair) == 2 for m, n in [pair]
            if m.distance < .75 * n.distance and backwards.get(m.trainIdx) == m.queryIdx]
    return (a.points[[m.queryIdx for m in good]], b.points[[m.trainIdx for m in good]])


def angle(rotation) -> float:
    return float(np.arccos(np.clip((np.trace(rotation) - 1.) / 2., -1., 1.)))


def _project(H, points):
    p = np.c_[points, np.ones(len(points))] @ H.T
    if np.any(np.abs(p[:, 2]) < 1e-9):
        raise ValueError("특이 투영")
    return p[:, :2] / p[:, 2, None]


def fit_rotation(p, q, K, size, config: RotationConfig) -> dict:
    """1/5 대응은 H 적합에서 제외한다. H 잔차와 회전 H 잔차를 별도로 검사한다."""
    result = {"status": "REQUIRES_SETUP", "matches": len(p), "reason": "매칭 부족"}
    if len(p) < config.min_matches:
        return result
    held = np.arange(len(p)) % 5 == 0
    H, inliers = cv2.findHomography(p[~held], q[~held], cv2.RANSAC, config.max_residual_px)
    if H is None or inliers is None or not np.isfinite(H).all():
        return {**result, "reason": "호모그래피 적합 실패"}
    K = np.asarray(K, dtype=float)
    M = np.linalg.solve(K, H @ K)
    if np.linalg.det(M) < 0:
        M = -M
    U, singular, Vt = np.linalg.svd(M)
    delta = U @ np.diag([1., 1., np.linalg.det(U @ Vt)]) @ Vt
    residual = np.linalg.norm(_project(H, p[held]) - q[held], axis=1)
    rotation_residual = np.linalg.norm(_project(K @ delta @ np.linalg.inv(K), p[held]) - q[held], axis=1)
    coverage = min(float(np.prod(np.ptp(x, axis=0)) / (size[0] * size[1])) for x in (p, q))
    result.update(delta_R=delta.tolist(), angle_rad=angle(delta),
                  singular_ratio=float(singular[-1] / singular[0]),
                  inlier_ratio=float(inliers.mean()), coverage=coverage,
                  residual_p95_px=float(np.percentile(residual, 95)),
                  rotation_residual_p95_px=float(np.percentile(rotation_residual, 95)))
    pure = (result["singular_ratio"] >= config.min_singular_ratio
            and result["inlier_ratio"] >= config.min_inlier_ratio and coverage >= config.min_coverage
            and max(result["residual_p95_px"], result["rotation_residual_p95_px"]) <= config.max_residual_px)
    if not pure:
        return {**result, "reason": "위치 이동/시차 또는 불충분한 정적 배경"}
    if result["angle_rad"] > config.max_angle_rad:
        return {**result, "reason": "자동 복구 허용각 초과"}
    return {**result, "status": "ROTATION", "reason": None}


def fixed_center_camera(reference, delta):
    camera = copy.deepcopy(reference)
    R = np.asarray(reference["R"], dtype=float)
    C = -R.T @ np.asarray(reference["t"], dtype=float)
    U, _, Vt = np.linalg.svd(np.asarray(delta, dtype=float) @ R)
    updated = U @ np.diag([1., 1., np.linalg.det(U @ Vt)]) @ Vt
    camera.update(R=updated.tolist(), t=(-updated @ C).tolist())
    # 이전 세팅의 재투영 오차는 새 자세의 검증값이 아니다.
    camera.pop("reprojection_error_px", None)
    return camera


def pair_reprojection(a, b, p, q):
    """같은 정적 대응의 두 영상 재투영 RMS. 음의 깊이는 큰 실패값으로 보존한다."""
    Pa = np.asarray(a["K"]) @ np.c_[a["R"], a["t"]]
    Pb = np.asarray(b["K"]) @ np.c_[b["R"], b["t"]]
    X = cv2.triangulatePoints(Pa.astype(float), Pb.astype(float), p.T, q.T).T
    valid = np.abs(X[:, 3]) > 1e-9
    xyz = X[:, :3] / np.where(valid, X[:, 3], 1)[:, None]
    errors = []
    for camera, observations in ((a, p), (b, q)):
        cam = xyz @ np.asarray(camera["R"]).T + np.asarray(camera["t"])
        valid &= cam[:, 2] > 1e-6
        projection = cam @ np.asarray(camera["K"]).T
        uv = projection[:, :2] / np.maximum(projection[:, 2, None], 1e-9)
        errors.append(np.sum((uv - observations) ** 2, axis=1))
    residual = np.sqrt((errors[0] + errors[1]) / 2.)
    residual[~valid | ~np.isfinite(residual)] = 1e6
    return {"median_px": float(np.median(residual)), "p95_px": float(np.percentile(residual, 95))}


def evaluate_pairs(previous, candidate, features, changed, config: RotationConfig, check_budget=lambda: None):
    old = {c["camera_id"]: c for c in previous["cameras"]}
    new = {c["camera_id"]: c for c in candidate["cameras"]}
    metrics = {}
    ids = sorted(features)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a not in changed and b not in changed:
                continue
            check_budget()
            p, q = match_features(features[a], features[b])
            if len(p) < config.min_matches:
                continue  # 대각 쌍은 원래 매칭되지 않는다. 인접 쌍을 자동 선택한다.
            _, mask = cv2.findFundamentalMat(p, q, cv2.FM_RANSAC, 2., .999)
            if mask is None or int(mask.sum()) < config.min_matches:
                continue
            p, q = p[mask.ravel() != 0], q[mask.ravel() != 0]
            metrics[f"{a}/{b}"] = {"camera_ids": [a, b], "matches": len(p),
                                     "before": pair_reprojection(old[a], old[b], p, q),
                                     "after": pair_reprojection(new[a], new[b], p, q)}
    return metrics


def pairs_improve(pairs, changed, config: RotationConfig) -> bool:
    for cid in changed:
        neighbors = [v for v in pairs.values() if cid in v["camera_ids"]]
        if not neighbors:
            return False
        before = sum(v["before"]["median_px"] for v in neighbors)
        after = sum(v["after"]["median_px"] for v in neighbors)
        if before < .5 * len(neighbors) or after > before * (1. - config.min_pair_gain):
            return False
    return all(v["matches"] >= config.min_matches and v["after"]["p95_px"] <= config.max_pair_p95_px
               and v["after"]["median_px"] <= v["before"]["median_px"] + config.max_pair_worsening_px
               for v in pairs.values())


def consistent_rotation(previous: Mapping[str, Any] | None, current: Mapping[str, Any], tolerance_rad: float) -> bool:
    if previous is None or previous.get("changed_camera_ids") != current.get("changed_camera_ids"):
        return False
    for cid in current.get("changed_camera_ids", ()):
        a = np.asarray(previous["rotations"][cid]["delta_R"], dtype=float)
        b = np.asarray(current["rotations"][cid]["delta_R"], dtype=float)
        if angle(b @ a.T) > tolerance_rad:
            return False
    return True


def make_candidate(previous, references, features, recent, *, at_ms, version, config, check_budget=lambda: None):
    document = copy.deepcopy(previous)
    rotations, changed, failed = {}, [], None
    for i, active in enumerate(document["cameras"]):
        cid = active["camera_id"]
        reference, ref_features = references[cid]
        check_budget()
        p, q = match_features(ref_features, features[cid])
        fit = fit_rotation(p, q, reference["K"], ref_features.size, config)
        rotations[cid] = fit
        if fit["status"] != "ROTATION":
            if failed is None:
                failed = cid, fit["reason"]
            continue
        camera = fixed_center_camera(reference, fit["delta_R"])
        change = angle(np.asarray(camera["R"]) @ np.asarray(active["R"]).T)
        fit["active_angle_rad"] = change
        if change >= config.min_angle_rad:
            document["cameras"][i] = camera
            changed.append(cid)
    end_ms = min(recent.values())
    if failed is not None:
        return None, {"status": "REQUIRES_SETUP", "reason": failed[1], "camera_id": failed[0],
                      "rotations": rotations, "validation_end_ms": end_ms, "recent_samples_ms": recent}
    if not changed:
        return None, {"status": "STABLE", "reason": "STABLE", "rotations": rotations,
                      "validation_end_ms": end_ms, "recent_samples_ms": recent}
    document.update(calibration_version=version, created_at=at_ms, state="PROVISIONAL")
    document.pop("validation", None)
    document["content_hash"] = content_hash(document)
    pairs = evaluate_pairs(previous, document, features, changed, config, check_budget)
    accepted = pairs_improve(pairs, changed, config)
    return document if accepted else None, {
        "status": "PROPOSED" if accepted else "REQUIRES_SETUP",
        "method": METHOD, "previous_version": previous["calibration_version"],
        "calibration": {"version": version, "content_hash": document["content_hash"]},
        "validation_end_ms": end_ms, "recent_samples_ms": recent,
        "camera_id": changed[0], "changed_camera_ids": changed, "rotations": rotations, "pairs": pairs,
        "world_alignment": {"mode": "fixed_centers_rotation_only", "fixed_center_rotation_cameras": changed},
        "thresholds": asdict(config), "reason": None if accepted else "인접 쌍 배경 재투영 개선 미달",
        "improvement_decision": {"accepted": accepted},
    }


def validate_rotation_candidate(document: Mapping[str, Any], report, previous):
    """운영 저장 경계: 보고서 결속, 실제 중심·World 보존, 배경 근거와 2회 확인을 검사한다."""
    from simai_ai.calibration.runtime import _validate

    _validate(document)
    config = RotationConfig(**report["thresholds"])
    if (report["previous_version"] != previous["calibration_version"]
            or document["state"] != "PROVISIONAL"
            or report["calibration"] != {"version": document["calibration_version"], "content_hash": document["content_hash"]}
            or report["consecutive_confirmation"]["count"] < 2
            or not report["improvement_decision"]["accepted"]):
        raise ValueError("회전 후보 버전/연속 확인 불일치")
    old = {c["camera_id"]: c for c in previous["cameras"]}
    new = {c["camera_id"]: c for c in document["cameras"]}
    changed = set(report["changed_camera_ids"])
    if not changed or not changed <= old.keys() or old.keys() != new.keys():
        raise ValueError("회전 후보 카메라 집합 불일치")
    for key in ("coordinate_frame", "world_axes_source", "scale_basis", "scale_cross_check_basis", "provider_source"):
        if document.get(key) != previous.get(key):
            raise ValueError("회전 후보 World/출처 변경")
    if not previous["coordinate_frame"].get("site_id") or previous["provider_source"] != "G1_AUTO":
        raise ValueError("회전 복구는 현장 보정만 허용")
    for cid, camera in new.items():
        prior = old[cid]
        for key in ("K", "distortion", "image_size"):
            if camera[key] != prior[key]:
                raise ValueError("회전 후보 내부 파라미터 변경")
        C = lambda c: -np.asarray(c["R"]).T @ np.asarray(c["t"])
        if not np.allclose(C(camera), C(prior), atol=1e-8, rtol=0):
            raise ValueError("회전 후보 카메라 중심 이동")
        if cid not in changed and camera != prior:
            raise ValueError("대상 밖 카메라 변경")
        if cid in changed:
            fit = report["rotations"][cid]
            values = [fit[k] for k in ("singular_ratio", "inlier_ratio", "coverage", "residual_p95_px",
                                       "rotation_residual_p95_px", "active_angle_rad")]
            if not all(math.isfinite(v) for v in values):
                raise ValueError("회전 후보 비유한 지표")
            actual_angle = angle(np.asarray(camera["R"]) @ np.asarray(prior["R"]).T)
            if (fit["status"] != "ROTATION" or fit["matches"] < config.min_matches
                    or fit["singular_ratio"] < config.min_singular_ratio
                    or fit["inlier_ratio"] < config.min_inlier_ratio or fit["coverage"] < config.min_coverage
                    or max(fit["residual_p95_px"], fit["rotation_residual_p95_px"]) > config.max_residual_px
                    or not config.min_angle_rad <= actual_angle <= config.max_angle_rad):
                raise ValueError("회전 후보 순수 회전 근거 미달")
    if not pairs_improve(report["pairs"], changed, config):
        raise ValueError("회전 후보 인접 쌍 개선 미달")
    return {"accepted": True, "method": METHOD, "changed_camera_ids": sorted(changed)}
