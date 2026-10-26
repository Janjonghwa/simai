package com.ssafy.simai.domain.tier0.i1;

import java.time.Instant;

/** Tier 0 health 하나를 저장했다(receivedAt 은 EC2 수신 시각). 화면 상태(viewer_health)가 원문과 수신 시각을 쓴다(명세 12.4). */
public record Tier0HealthStored(I1Envelope envelope, Instant receivedAt) {
}
