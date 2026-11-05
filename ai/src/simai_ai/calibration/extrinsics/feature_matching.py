"""CPU 배경 특징점 매칭. 반환 좌표는 입력 이미지 픽셀이다."""

import cv2
import numpy as np


def match_sift(image_a: np.ndarray, image_b: np.ndarray) -> list[dict]:
    sift = cv2.SIFT_create()
    def features(image):
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
        return sift.detectAndCompute(gray, None)

    ka, da = features(image_a)
    kb, db = features(image_b)
    if da is None or db is None or min(len(da), len(db)) < 2:
        return []
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    def ratio_matches(first, second):
        return {m.queryIdx: m.trainIdx for pair in matcher.knnMatch(first, second, k=2)
                if len(pair) == 2 for m, n in [pair] if m.distance < 0.8 * n.distance}

    forward, reverse = ratio_matches(da, db), ratio_matches(db, da)
    return [{"uv": [ka[i].pt, kb[j].pt], "nodes": [[0, i], [1, j]]}
            for i, j in forward.items() if reverse.get(j) == i]
