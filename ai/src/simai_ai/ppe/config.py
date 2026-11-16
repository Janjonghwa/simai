"""PPE 연결·누적 규칙의 수치 (명세 5.5). 모두 초기값이며 REPLAY·교실(=녹화 로비) 실측으로 고친다.

명세에 있는 값은 출처를 적었다. 나머지는 SimAI 판정 로직 초안(reviews/ppe-track-judgement-design-20261105-v1)의 제안값이고,
바꾸면 `POLICY_VERSION` 을 올린다(ppe_state.policy_version, 재현용).
"""

from __future__ import annotations

from dataclasses import dataclass

# 0.2.0: ID 불확실 칸 건너뛰기·3초 초과 초기화·유효 6칸 5초 창(S15P31A101-653)
# 0.3.0: 미착용 창에 빈 칸 1칸 허용(not_worn_max_empty_bins, S15P31A101-654)
# 0.4.0: 순수 병합(aliases 추가·분할 아님)이면 차수가 올라도 누적을 잇고 흡수 ID 칸을 넘겨받는다(S15P31A101-661)
# 0.5.0: ID 초기화 사유는 초기화 칸까지만 남기고 다음 칸부터 칸 사유로 바꾼다. 상태·수치는 0.4.0 과 같다(S15P31A101-729)
# 0.6.0: 두 어깨가 보이면 머리 폭 하한 = 어깨 폭 × 0.6(head_min_scale_shoulder_ratio, S15P31A101-737)
# 0.7.0: 착용·미착용 카메라가 섞인 칸은 미착용 카메라가 2대 이상일 때만 충돌, 1대면 착용 칸(conflict_min_not_worn_cameras, S15P31A101-766)
POLICY_VERSION = "ppe-policy-0.7.0"


@dataclass(frozen=True)
class PpeConfig:
    # keypoint(Tier 0 관측 2D, COCO17 [u, v, confidence])
    min_keypoint_confidence: float = 0.3        # 삼각측량 관절 기준과 같게(제안)
    # 머리 영역(눈·귀, 5.5)
    head_min_anchor_points: int = 2             # 눈·귀 4점 중 최소 개수
    head_clear_min_points: int = 3              # 미착용 근거로 쓸 '선명한 머리': 코·눈·귀 5점 중 최소 개수
    head_clear_min_scale_px: float = 12.0       # 선명한 머리의 원본 픽셀 머리 폭 하한
    head_min_scale_px: float = 8.0
    # 두 어깨가 보이면 머리 폭 하한 = 어깨 폭 × 이 값(0 이면 끔). 14차 로비 실측: 검출 없음 200명 중 180명은 머리에 걸친 상자가
    # 있었으나 눈·귀 폭이 작아(머리 폭/키 0.07, 정상 0.11) 연결되지 않았다. 0.6 에서 80명 회복·대조 99/100 유지·직접 확인 24/24
    # (S15P31A101-737, docs/design/S15P31A101-737-ppe-head-scale-shoulder.md)
    head_min_scale_shoulder_ratio: float = 0.6
    eye_span_to_head_width: float = 2.5         # 귀가 안 보일 때 눈 간격 → 머리 폭
    side_span_to_head_width: float = 2.0        # 귀 1개 + 눈/코 → 머리 폭
    head_expand_side: float = 0.8               # 머리 폭 배수(중심에서 좌우)
    head_expand_up: float = 1.4                 # 눈·귀 높이에서 위로(안전모 챙·정수리)
    head_expand_down: float = 0.6               # 눈·귀 높이에서 아래로(턱)
    helmet_width_ratio_range: tuple[float, float] = (0.6, 3.0)  # 검출 폭 / 머리 폭
    # 몸통 영역(어깨~골반, 5.5)
    torso_require_both_shoulders: bool = True
    torso_min_hips: int = 1
    torso_expand_side: float = 0.25
    torso_expand_vertical: float = 0.15
    # 일대일 배정
    match_det_coverage: float = 0.6             # 검출 상자 중 영역 안 비율 ≥ 이면 '겹침'
    vest_region_coverage: float = 0.3           # 조끼: 몸통 영역 중 검출이 덮는 비율 ≥
    touch_det_coverage: float = 0.2             # 이 비율 이상 걸린 사람이 둘 이상이면 모호(UNKNOWN)
    # 시간 누적(5.5: 유효 6칸 창, 10초 만료)
    bin_ms: int = 500
    window_bins: int = 6                        # 확정에 쓰는 유효 칸 수(ID 불확실 칸은 세지 않는다)
    window_span_bins: int = 10                  # 유효 6칸을 찾는 최근 범위(5초). 오래된 근거로 확정하지 않게
    uncertain_reset_ms: int = 3_000             # ID 불확실이 이보다 길게 이어지면 상태를 초기화한다(S15P31A101-653, D2)
    worn_min_bins: int = 5
    # 미착용 창(6칸)에 허용하는 빈 칸(표본 없음: 프레임 드롭·추론 건너뜀) 수. 마지막 칸은 미착용이어야 한다(S15P31A101-654,
    # 14차 로비: 빈 칸 하나에 창 전체가 리셋돼 미착용이 확정되지 않았다). 착용·충돌·모호 칸은 그대로 막는다
    not_worn_max_empty_bins: int = 1
    # 한 칸에 착용 카메라와 미착용 카메라가 함께면, 미착용 카메라가 이 수 이상일 때만 충돌 칸이고 그보다 적으면 착용 칸이다.
    # 2026-11-16 로비 실측: 검은 조끼 착용자는 카메라 4대 모두에서 프레임의 15~28% 가 미착용으로 나와(한 카메라 문제 아님)
    # 칸의 30% 가 충돌이었다. 2대 이상이면 충돌 30%→9%, 조끼 없는 구간 미착용 칸 85% 유지(S15P31A101-766,
    # docs/design/S15P31A101-766-ppe-bin-two-camera-conflict.md). 1 이면 0.6.0 까지의 규칙(섞이면 충돌)과 같다
    conflict_min_not_worn_cameras: int = 2
    max_camera_skew_ms: int = 100
    bin_close_grace_ms: int = 100
    expiry_ms: int = 10_000
    mapping_ttl_ms: int = 2_000                 # 로컬 ID 주인 기억(World Track key_ttl 2 s 와 같게)
    track_retention_ms: int = 30_000
    evidence_max_items: int = 4
    # 모호한 근거가 있는 칸은 미착용으로 세지 않는다(착용 쪽으로 기우는 초기값. 엉뚱한 미착용 경보 vs 놓친 미착용 결정 전)
    ambiguity_blocks_not_worn: bool = True


DEFAULT_CONFIG = PpeConfig()
