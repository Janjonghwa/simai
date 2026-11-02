package com.ssafy.simai.watch.alert

/** I4 watch_ack 의 kind. VIBRATED 는 진동을 시작하면 자동으로(격상 판단 기준), CONFIRMED 는 작업자가 확인 단추를 누르면 */
enum class AckKind { VIBRATED, CONFIRMED }

/**
 * EC2 로 보낼 확인 한 건(I4 watch_ack 의 payload 값). 시각은 워치 시계(UTC ms).
 *
 * @property receivedAtMs 알림을 받은 워치 시각. VIBRATED 에만 싣는다(전달 지연 측정용)
 */
data class AckRecord(
    val alertMsgId: String,
    val eventId: String,
    val eventSequence: Long?,
    val kind: AckKind,
    val ackedAtMs: Long,
    val receivedAtMs: Long?,
) {
    /** EC2 가 중복을 제거하는 키와 같다(계약 watch_ack: alert_msg_id + kind) */
    val key: Pair<String, AckKind> get() = alertMsgId to kind
}
