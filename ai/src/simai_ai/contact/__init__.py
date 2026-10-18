"""ContactEstimator: 사람과 지지 표면의 접촉 추정 (명세 5.2 접지 추정, 7.5 작업면).

| 모듈 | 내용 |
|---|---|
| surfaces | 지지 표면 후보: 바닥(Z=0)과 활성 구성 묶음 Operational Geometry 의 등록 작업면(OBB, 상판 높이) |
| estimator | ContactEstimator: VALID 발목 높이·정지 여부와 후보 높이로 지지 표면·접지점 추정, 가림 동안 유지(시간 상한) |

결과(`ContactFrame.contacts()`)는 `tracking.world_state.world_state_payload(..., contacts=...)` 에 넘겨 I1 world_state 의
ground_anchor_xyz·support_surface_*·contact_validity·z_validity 를 채운다.
"""

from simai_ai.contact.estimator import (
    ContactConfig,
    ContactEstimate,
    ContactEstimator,
    ContactFrame,
    ContactReason,
    SurfaceSource,
)
from simai_ai.contact.surfaces import (
    DEFAULT_SURFACE_SIGMA_M,
    FLOOR,
    FLOOR_ID,
    FLOOR_SIGMA_Z_M,
    SupportSurface,
    SurfaceSet,
)

__all__ = [
    "DEFAULT_SURFACE_SIGMA_M",
    "FLOOR",
    "FLOOR_ID",
    "FLOOR_SIGMA_Z_M",
    "ContactConfig",
    "ContactEstimate",
    "ContactEstimator",
    "ContactFrame",
    "ContactReason",
    "SupportSurface",
    "SurfaceSet",
    "SurfaceSource",
]
