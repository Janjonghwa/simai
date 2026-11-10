"""배경만 쓰는 중심 고정 회전 후보. World→camera, 미터·라디안, 시각은 호출자가 준다."""

from __future__ import annotations

import copy
import math
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from typing import Any, Mapping

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
    min_valid_ratio: float = 0.5
    max_person_cover_ratio: float = 0.10
    max_unrevealed_ratio: float = 0.02
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
        if any(
            getattr(self, k) > 1
            for k in (
                "min_singular_ratio",
                "min_inlier_ratio",
                "min_coverage",
                "min_valid_ratio",
                "max_person_cover_ratio",
                "max_unrevealed_ratio",
                "min_pair_gain",
            )
        ):
            raise ValueError("rotation_recovery: 비율은 1 이하")


@dataclass(frozen=True)
class Features:
    points: np.ndarray  # 왜곡 제거한 원본 해상도의 픽셀 좌표
    descriptors: np.ndarray | None
    size: tuple[int, int]
    valid_ratio: float = 1.0
    mask: np.ndarray | None = None
    person_cover_ratio: float = 0.0
    never_revealed_ratio: float = 0.0


def extract_features(
    image,
    mask,
    camera,
    config: RotationConfig,
    *,
    original_size=None,
    person_cover_ratio: float = 0.0,
    never_revealed_ratio: float = 0.0,
) -> Features:
    import cv2

    source_size = tuple(image.shape[1::-1]) if original_size is None else tuple(original_size)
    if source_size != tuple(camera["image_size"]):
        raise ValueError("STREAM_CHANGED: 배경과 내부 파라미터의 해상도가 다름")
    h, w = image.shape[:2]
    scale = min(1., config.max_image_side / max(w, h))
    size = (round(w * scale), round(h * scale))
    small = cv2.resize(image, size, interpolation=cv2.INTER_LINEAR)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    valid = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST)
    valid_ratio = float((valid > 0).mean()) if valid.size else 0.0
    # 마스크 경계의 descriptor patch도 관측된 정적 배경만 쓴다.
    valid = cv2.erode(valid, np.ones((9, 9), np.uint8))
    sift = cv2.SIFT_create(nfeatures=config.max_features)
    keypoints, descriptors = sift.detectAndCompute(gray, valid)
    if len(keypoints) < max(80, config.min_matches * 2) and (valid > 0).any():
        active_px = gray[valid > 0]
        lo_p, hi_p = float(np.percentile(active_px, 2)), float(np.percentile(active_px, 98))
        if hi_p - lo_p >= 8.0:
            norm_gray = np.clip((gray.astype(np.float32) - lo_p) * (255.0 / (hi_p - lo_p)), 0.0, 255.0).astype(np.uint8)
            kp2, desc2 = sift.detectAndCompute(norm_gray, valid)
            if len(kp2) > len(keypoints):
                keypoints, descriptors = kp2, desc2
    points = np.array([k.pt for k in keypoints], dtype=np.float64).reshape(-1, 2)
    points *= np.array([source_size[0] / size[0], source_size[1] / size[1]])
    distortion = camera["distortion"]
    if distortion["model"] not in ("NONE", "OPENCV", "OPENCV_PINHOLE", "OPENCV_FISHEYE"):
        raise ValueError("지원하지 않는 렌즈 왜곡 모델")
    if len(points):
        K = np.asarray(camera["K"], dtype=float)
        coeffs = np.asarray(distortion["coeffs"], dtype=float)
        if distortion["model"] == "OPENCV_FISHEYE":
            points = cv2.fisheye.undistortPoints(points[:, None], K, coeffs, P=K)[:, 0]
        elif distortion["model"] != "NONE":
            points = cv2.undistortPoints(points[:, None], K, coeffs if coeffs.size else None, P=K)[:, 0]
    return Features(
        points,
        descriptors,
        source_size,
        valid_ratio,
        valid,
        float(person_cover_ratio),
        float(never_revealed_ratio),
    )


def match_features(a: Features, b: Features):
    if a.descriptors is None or b.descriptors is None or min(len(a.points), len(b.points)) < 2:
        return np.empty((0, 2)), np.empty((0, 2))
    import cv2

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


def fit_rotation(
    p,
    q,
    K,
    size,
    config: RotationConfig,
    *,
    valid_ratio: float = 1.0,
    person_cover_ratio: float = 0.0,
    never_revealed_ratio: float = 0.0,
) -> dict:
    """1/5 대응은 H 적합에서 제외한다. H 잔차와 회전 H 잔차를 별도로 검사한다."""
    crowd_occluded = (
        valid_ratio < config.min_valid_ratio
        or person_cover_ratio >= config.max_person_cover_ratio
        or never_revealed_ratio >= config.max_unrevealed_ratio
    )
    result = {
        "status": "REQUIRES_SETUP",
        "matches": len(p),
        "reason": "매칭 부족",
        "valid_ratio": float(valid_ratio),
        "person_cover_ratio": float(person_cover_ratio),
        "never_revealed_ratio": float(never_revealed_ratio),
    }
    if len(p) < config.min_matches:
        if len(p) >= 12 and valid_ratio < 0.90:
            disp = np.linalg.norm(p - q, axis=1)
            p95 = float(np.percentile(disp, 95))
            if float(np.percentile(disp, 90)) <= config.max_residual_px:
                return {
                    **result,
                    "status": "ROTATION",
                    "delta_R": np.eye(3).tolist(),
                    "angle_rad": 0.0,
                    "singular_ratio": 1.0,
                    "inlier_ratio": float((disp <= config.max_residual_px).mean()),
                    "coverage": 0.0,
                    "residual_p95_px": p95,
                    "rotation_residual_p95_px": p95,
                    "reason": None,
                }
        if crowd_occluded or (
            len(p) >= 8 and float(np.median(np.linalg.norm(p - q, axis=1))) <= config.max_residual_px * 1.5
        ):
            return {**result, "status": "NO_EVIDENCE", "reason": "가림 또는 유효 배경 부족"}
        return result
    held = np.arange(len(p)) % 5 == 0
    import cv2

    H, inliers = cv2.findHomography(p[~held], q[~held], cv2.RANSAC, config.max_residual_px)
    if H is None or inliers is None or not np.isfinite(H).all():
        if crowd_occluded:
            return {**result, "status": "NO_EVIDENCE", "reason": "가림 또는 유효 배경 부족"}
        return {**result, "reason": "호모그래피 적합 실패"}
    K = np.asarray(K, dtype=float)
    M = np.linalg.solve(K, H @ K)
    if np.linalg.det(M) < 0:
        M = -M
    U, singular, Vt = np.linalg.svd(M)
    delta = U @ np.diag([1., 1., np.linalg.det(U @ Vt)]) @ Vt
    residual = np.linalg.norm(_project(H, p[held]) - q[held], axis=1)
    rotation_residual = np.linalg.norm(_project(K @ delta @ np.linalg.inv(K), p[held]) - q[held], axis=1)
    inlier_ratio = float(inliers.mean())
    # 부분 가림으로 소수의 외점 대응이 held-out에 섞여도 대다수 대응이 정합하면 인라이어 잔차로 평가한다.
    if (
        inlier_ratio >= config.min_inlier_ratio
        and float((residual <= config.max_residual_px).mean()) >= max(config.min_inlier_ratio, 0.75)
        and np.any(residual > config.max_residual_px * 3.0)
    ):
        keep = residual <= config.max_residual_px * 3.0
        residual = residual[keep]
        rotation_residual = rotation_residual[keep]
    coverage = min(float(np.prod(np.ptp(x, axis=0)) / (size[0] * size[1])) for x in (p, q))
    result.update(delta_R=delta.tolist(), angle_rad=angle(delta),
                  singular_ratio=float(singular[-1] / singular[0]),
                  inlier_ratio=inlier_ratio, coverage=coverage,
                  residual_p95_px=float(np.percentile(residual, 95)),
                  rotation_residual_p95_px=float(np.percentile(rotation_residual, 95)))
    geom_ok = (result["singular_ratio"] >= config.min_singular_ratio
               and result["inlier_ratio"] >= config.min_inlier_ratio
               and max(result["residual_p95_px"], result["rotation_residual_p95_px"]) <= config.max_residual_px)
    if geom_ok and coverage < config.min_coverage:
        if result["angle_rad"] < config.min_angle_rad:
            return {**result, "status": "ROTATION", "reason": None}
        if result["angle_rad"] <= config.max_angle_rad or crowd_occluded:
            return {**result, "status": "NO_EVIDENCE", "reason": "정적 배경 분포 면적 부족"}
    pure = geom_ok and coverage >= config.min_coverage
    if not pure:
        disp = np.linalg.norm(p - q, axis=1)
        stationary_anchors = int((disp <= config.max_residual_px).sum())
        stationary_ratio = float((disp <= config.max_residual_px).mean())
        if (
            crowd_occluded
            or coverage < config.min_coverage
            or (stationary_anchors >= 12 and stationary_ratio >= 0.35)
        ):
            return {**result, "status": "NO_EVIDENCE", "reason": "가림 또는 유효 배경 부족"}
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
    import cv2

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


def evaluate_pairs(previous, candidate, features, changed, config: RotationConfig, check_budget=lambda: None,
                   measure=lambda _stage: nullcontext()):
    old = {c["camera_id"]: c for c in previous["cameras"]}
    new = {c["camera_id"]: c for c in candidate["cameras"]}
    metrics = {}
    ids = sorted(features)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a not in changed and b not in changed:
                continue
            check_budget()
            with measure("matching"):
                p, q = match_features(features[a], features[b])
            if len(p) < config.min_matches:
                continue  # 대각 쌍은 원래 매칭되지 않는다. 인접 쌍을 자동 선택한다.
            import cv2

            with measure("estimation"):
                _, mask = cv2.findFundamentalMat(p, q, cv2.FM_RANSAC, 2., .999)
            if mask is None or int(mask.sum()) < config.min_matches:
                continue
            p, q = p[mask.ravel() != 0], q[mask.ravel() != 0]
            with measure("estimation"):
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


def make_candidate(previous, references, features, recent, *, at_ms, version, config, check_budget=lambda: None,
                   measure=lambda _stage: nullcontext(), ignored_cameras=()):
    document = copy.deepcopy(previous)
    ignored = set(ignored_cameras or ())
    rotations, changed = {}, []
    failed_cameras: dict[str, str] = {}
    no_evidence_cameras: dict[str, str] = {}
    for i, active in enumerate(document["cameras"]):
        cid = active["camera_id"]
        if cid in ignored:
            continue
        reference, ref_features = references[cid]
        feat = features[cid]
        check_budget()
        with measure("matching"):
            p, q = match_features(ref_features, feat)
        eff_valid_ratio = float(getattr(feat, "valid_ratio", 1.0))
        feat_mask = getattr(feat, "mask", None)
        if feat_mask is not None and feat_mask.size and len(ref_features.points):
            mh, mw = feat_mask.shape[:2]
            sw, sh = ref_features.size
            u_idx = np.clip((ref_features.points[:, 0] * (mw / sw)).astype(int), 0, mw - 1)
            v_idx = np.clip((ref_features.points[:, 1] * (mh / sh)).astype(int), 0, mh - 1)
            ref_vis = float((feat_mask[v_idx, u_idx] > 0).mean())
            eff_valid_ratio = min(eff_valid_ratio, ref_vis)
        with measure("estimation"):
            fit = fit_rotation(
                p,
                q,
                reference["K"],
                ref_features.size,
                config,
                valid_ratio=eff_valid_ratio,
                person_cover_ratio=float(getattr(feat, "person_cover_ratio", 0.0)),
                never_revealed_ratio=float(getattr(feat, "never_revealed_ratio", 0.0)),
            )
        rotations[cid] = fit
        if fit["status"] == "NO_EVIDENCE":
            no_evidence_cameras[cid] = str(fit["reason"])
            continue
        if fit["status"] != "ROTATION":
            failed_cameras[cid] = str(fit["reason"])
            continue
        camera = fixed_center_camera(reference, fit["delta_R"])
        change = angle(np.asarray(camera["R"]) @ np.asarray(active["R"]).T)
        fit["active_angle_rad"] = change
        if change >= config.min_angle_rad:
            document["cameras"][i] = camera
            changed.append(cid)
    active_recent = {cid: ms for cid, ms in recent.items() if cid not in ignored} or dict(recent)
    end_ms = min(active_recent.values()) if active_recent else at_ms
    if failed_cameras:
        first_cid, first_reason = next(iter(failed_cameras.items()))
        return None, {"status": "REQUIRES_SETUP", "reason": first_reason, "camera_id": first_cid,
                      "failed_cameras": failed_cameras, "rotations": rotations,
                      "validation_end_ms": end_ms, "recent_samples_ms": recent}
    if no_evidence_cameras:
        first_cid, first_reason = next(iter(no_evidence_cameras.items()))
        return None, {"status": "NO_EVIDENCE", "reason": first_reason, "camera_id": first_cid,
                      "no_evidence_cameras": no_evidence_cameras, "rotations": rotations,
                      "validation_end_ms": end_ms, "recent_samples_ms": recent}
    if not changed:
        return None, {"status": "STABLE", "reason": "STABLE", "rotations": rotations,
                      "validation_end_ms": end_ms, "recent_samples_ms": recent}
    document.update(calibration_version=version, created_at=at_ms, state="PROVISIONAL")
    document.pop("validation", None)
    document["content_hash"] = content_hash(document)
    pairs = evaluate_pairs(previous, document, features, changed, config, check_budget, measure)
    accepted = pairs_improve(pairs, changed, config)
    pair_reason = "인접 쌍 배경 재투영 개선 미달"
    return document if accepted else None, {
        "status": "PROPOSED" if accepted else "REQUIRES_SETUP",
        "method": METHOD, "previous_version": previous["calibration_version"],
        "calibration": {"version": version, "content_hash": document["content_hash"]},
        "validation_end_ms": end_ms, "recent_samples_ms": recent,
        "camera_id": changed[0], "changed_camera_ids": changed, "rotations": rotations, "pairs": pairs,
        "failed_cameras": {} if accepted else {cid: pair_reason for cid in changed},
        "world_alignment": {"mode": "fixed_centers_rotation_only", "fixed_center_rotation_cameras": changed},
        "thresholds": asdict(config), "reason": None if accepted else pair_reason,
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


def __getattr__(name: str) -> Any:
    if name == "cv2":
        import cv2

        return cv2
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
