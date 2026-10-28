"""번들 조정이 쓰는 관절 배치와 뼈 (COCO17 + 선택 발 끝점 2개).

- keypoint 순서는 contracts common joint_layout COCO17 이다. 발 끝점(l_toe, r_toe)이 있으면 17, 18 번에 붙인다
  (YOLO26 발 6점 미세조정의 발 끝, 시뮬레이터 `foot_tips_2d` 와 같은 순서).
- 뼈 길이 사전은 좌우 대칭인 뼈가 길이 변수 하나를 함께 쓴다(사람마다 한 벌). 길이 자체는 모르므로 변수로 두고,
  표본마다 같은 길이를 유지하게만 한다(HumanCalib 의 뼈 길이 일관성과 같은 원리). 그래서 뼈 사전은 스케일을 정하지 않는다.
- 얼굴 관절(코·눈·귀)은 뼈 사전에 넣지 않는다. 짧고 검출 잡음이 커 사전이 정보를 거의 더하지 않는다.
- 관절별 픽셀 잡음 배율(`KEYPOINT_NOISE_SCALE`)은 COCO keypoint 평가의 관절별 표준편차 κ(사람 표기 흔들림, OKS 정의)를
  눈 κ(0.025)로 나눈 값이다. 엉덩이는 눈의 4.3배, 어깨는 3.2배 흔들린다. 로비 걷기 실측 잔차(-313)도 같은 비율이었다
  (κ 로 나눈 관절별 잔차 중앙값 0.9~1.6). 발 끝은 COCO-WholeBody 엄지발가락 κ 0.068 을 쓴다.
"""

from __future__ import annotations

COCO17_COUNT = 17
WITH_FOOT_TIPS_COUNT = 19
SUPPORTED_COUNTS = (COCO17_COUNT, WITH_FOOT_TIPS_COUNT)

L_SHOULDER, R_SHOULDER = 5, 6
L_ELBOW, R_ELBOW = 7, 8
L_WRIST, R_WRIST = 9, 10
L_HIP, R_HIP = 11, 12
L_KNEE, R_KNEE = 13, 14
L_ANKLE, R_ANKLE = 15, 16
L_TOE, R_TOE = 17, 18

# 뼈 길이 변수 이름 → 그 길이를 쓰는 관절 쌍들(좌우 대칭은 한 변수)
BONE_GROUPS: dict[str, tuple[tuple[int, int], ...]] = {
    "upper_arm": ((L_SHOULDER, L_ELBOW), (R_SHOULDER, R_ELBOW)),
    "forearm": ((L_ELBOW, L_WRIST), (R_ELBOW, R_WRIST)),
    "thigh": ((L_HIP, L_KNEE), (R_HIP, R_KNEE)),
    "shin": ((L_KNEE, L_ANKLE), (R_KNEE, R_ANKLE)),
    "torso_side": ((L_SHOULDER, L_HIP), (R_SHOULDER, R_HIP)),
    "shoulder_width": ((L_SHOULDER, R_SHOULDER),),
    "hip_width": ((L_HIP, R_HIP),),
    "foot": ((L_ANKLE, L_TOE), (R_ANKLE, R_TOE)),
}
BONE_NAMES: tuple[str, ...] = tuple(BONE_GROUPS)

# 관절별 픽셀 잡음 배율 = COCO κ / 눈 κ(0.025). 순서는 COCO17 + 발 끝 2개
_COCO_KAPPA = (0.026, 0.025, 0.025, 0.035, 0.035, 0.079, 0.079, 0.072, 0.072, 0.062, 0.062, 0.107, 0.107, 0.087, 0.087,
               0.089, 0.089, 0.068, 0.068)
KEYPOINT_NOISE_SCALE: tuple[float, ...] = tuple(round(k / 0.025, 4) for k in _COCO_KAPPA)

# 발: (발목, 발 끝) 왼쪽·오른쪽
FEET: tuple[tuple[int, int], ...] = ((L_ANKLE, L_TOE), (R_ANKLE, R_TOE))


def bones_for(joint_count: int) -> list[tuple[str, int, int]]:
    """이 관절 수에서 쓸 수 있는 뼈 (변수 이름, 관절 a, 관절 b)."""
    return [
        (name, a, b) for name, pairs in BONE_GROUPS.items() for a, b in pairs if a < joint_count and b < joint_count
    ]
