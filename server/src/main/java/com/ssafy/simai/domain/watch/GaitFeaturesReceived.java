package com.ssafy.simai.domain.watch;

import java.time.Instant;
import tools.jackson.databind.JsonNode;

/** 계약 검사를 지난 gait_features 한 창(S15P31A101-340). WatchHub 가 받은 WebSocket 스레드에서 내고 WatchMotionMatcher 가 받는다 */
record GaitFeaturesReceived(String deviceId, JsonNode payload, Instant receivedAt) {
}
