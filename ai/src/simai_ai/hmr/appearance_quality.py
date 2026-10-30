"""외형 관측 품질만 평가한다. Tier 0 위치·관절·판정은 수정하지 않는다."""

import numpy as np

from simai_ai.hmr.multiview import observed_coco17, reprojection_px


def silhouette_weight(mask, cfg):
    """원본 해상도의 이상적 픽셀 거리. 침식 띠를 버리고 그 안쪽에서 선형 회복."""
    if not cfg.silhouette_enabled:
        return None
    import cv2

    distance = cv2.distanceTransform(np.pad(mask, 1), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)[1:-1, 1:-1]
    return np.clip((distance - cfg.silhouette_erode_px) / cfg.silhouette_fade_px, 0, 1)


def blur_weight(image, mask, history, cfg):
    if not cfg.blur_enabled:
        return 1., None
    import cv2

    # 경계의 배경 대비를 선명도로 세지 않는다. Laplacian의 3×3 이웃도 내부여야 한다.
    inside = cv2.erode(mask.astype(np.uint8), np.ones((3, 3), np.uint8),
                       borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
    if np.count_nonzero(inside) < cfg.blur_min_pixels:
        return cfg.blur_floor, None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    variance = float(cv2.Laplacian(gray, cv2.CV_32F, ksize=1)[inside].var())
    baseline = float(np.median(history)) if history else variance
    # 무텍스처(분산 0) 기준으로는 흐림을 알 수 없어 감쇠하지 않는다.
    weight = float(np.clip(variance / baseline, cfg.blur_floor, 1)) if baseline > 1e-6 else 1.
    history.append(variance)
    del history[:-cfg.blur_history]
    return weight, variance


def fit_weight(view, joints_world, cfg):
    if not cfg.fit_enabled:
        return 1., None, 0
    if joints_world is None or view.keypoints_2d is None:
        return cfg.fit_sparse_weight, None, 0
    ideal, _ = observed_coco17(view.camera, view.keypoints_2d, cfg.fit_min_confidence)
    errors = reprojection_px(view.camera, joints_world, ideal)
    errors = errors[np.isfinite(errors)]
    error = float(np.median(errors)) if len(errors) else None
    if len(errors) < cfg.fit_min_joints:
        return cfg.fit_sparse_weight, error, len(errors)
    weight = float(np.clip((cfg.fit_reject_px - error) / (cfg.fit_reject_px - cfg.fit_full_px), 0, 1))
    return weight, error, len(errors)


class CameraColors:
    """pipeline의 모든 사람이 공유한다. 호출자가 회차 성공 뒤에만 commit한다."""

    def __init__(self):
        self.gains = {}

    def gain(self, camera):
        return self.gains.get(camera, np.ones(3))

    def copy(self):
        other = CameraColors()
        other.gains = {k: v.copy() for k, v in self.gains.items()}
        return other

    def commit(self, other):
        self.gains = other.gains

    def estimate(self, samples, cfg):
        if len(samples) < 2:
            return
        # 최다 고품질 텍셀 카메라가 기준. 동률은 ID 순. 기존 기준 이득을 유지해 기준 전환 시 연속성 확보.
        reference = min(samples, key=lambda c: (-len(samples[c][0]), c.camera_id))
        ri, rgb = samples[reference]
        reference_gain = self.gain(reference)
        updates = {}
        for camera, (indices, colors) in samples.items():
            if camera == reference:
                continue
            _, a, b = np.intersect1d(ri, indices, assume_unique=True, return_indices=True)
            left, right = rgb[a], colors[b]
            usable = ((left >= cfg.color_min_rgb) & (left <= cfg.color_max_rgb)
                      & (right >= cfg.color_min_rgb) & (right <= cfg.color_max_rgb)).all(axis=1)
            if np.count_nonzero(usable) < cfg.color_min_common:
                continue
            ratio = np.exp(np.median(np.log(left[usable]) - np.log(right[usable]), axis=0))
            target = np.clip(reference_gain * ratio, cfg.color_gain_min, cfg.color_gain_max)
            updates[camera] = np.clip((1 - cfg.color_ema) * self.gain(camera) + cfg.color_ema * target,
                                      cfg.color_gain_min, cfg.color_gain_max)
        self.gains.update(updates)
