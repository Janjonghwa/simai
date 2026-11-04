package com.ssafy.simai.domain.watch;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import java.time.Instant;

/**
 * 워치가 보낸 watch_ack(계약 검사를 지남). WatchHub 가 받은 스레드에서 발행하고 WatchAlertService 가 기록한다(S15P31A101-338).
 * 둘이 서로를 부르면 빈 순환이 생겨 이벤트로 넘긴다.
 *
 * @param deviceId 이 연결을 연 워치(봉투 source_id 와 같음을 WatchHub 가 확인했다)
 * @param receivedAt EC2 가 받은 시각. 격상 판단은 이 시각으로만 한다(워치 시계 acked_at 은 기록용)
 */
record WatchAckReceived(String deviceId, I1Envelope envelope, Instant receivedAt) {
}
