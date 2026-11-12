package com.ssafy.simai.watch.alert

/** 한 번에 들고 있는 알림 수의 상한. 넘으면 확인한 것부터, 그다음 가장 오래된 것부터 버린다 */
const val MAX_KEPT_ALERTS = 32

enum class AlertPhase { ACTIVE, EXPIRED, ACKED }

/**
 * 받은 알림 하나의 상태.
 * ACTIVE 는 확인을 기다리는 중(진동), EXPIRED 는 확인 기한이 지난 것(진동은 멈추고 확인은 계속 받는다), ACKED 는 확인한 것.
 *
 * @property receivedAtMs 워치가 받은 시각. 기한은 여기서부터 잰다. 벽시계가 아니라 단조 시계(부팅 뒤 경과)다:
 *   NTP 보정으로 시계가 뛰어도 기한이 갑자기 지나거나 늘어나지 않게 한다
 * @property ackedAtMs 확인한 시각(UTC ms 벽시계). 화면에 "확인함 시각" 으로 보이고 EC2 로 보내는 값이다
 */
data class TrackedAlert(
    val alert: WatchAlert,
    val receivedAtMs: Long,
    val ackedAtMs: Long? = null,
    val expired: Boolean = false,
) {
    val phase: AlertPhase
        get() = when {
            ackedAtMs != null -> AlertPhase.ACKED
            expired -> AlertPhase.EXPIRED
            else -> AlertPhase.ACTIVE
        }

    val deadlineMs: Long get() = receivedAtMs + alert.ackWithinMs

    /** 확인 기한까지 남은 비율(1 에서 0 으로 줄어든다). 기한이 지났으면 0. [nowMs] 는 [receivedAtMs] 와 같은 단조 시계다 */
    fun remainingFraction(nowMs: Long): Float = ((deadlineMs - nowMs).toFloat() / alert.ackWithinMs).coerceIn(0f, 1f)

    /** 기한이 지난 뒤 흐른 시간(ms). 기한 전이면 0 */
    fun overdueMs(nowMs: Long): Long = (nowMs - deadlineMs).coerceAtLeast(0L)
}

/**
 * 워치가 들고 있는 알림 모음. 시계를 직접 읽지 않고 시각을 받는 순수한 값이라 JVM 에서 시험한다.
 *
 * 화면에는 [current] 하나를 보이고 나머지는 [others] 건으로 센다. 진동은 [vibrating] 이 낸다.
 */
class AlertBoard private constructor(val items: List<TrackedAlert>) {
    constructor() : this(emptyList())

    /**
     * 아직 확인하지 않은 알림. 기한 안(ACTIVE, 지금 울리는 쪽)이 먼저, 그 안에서 급한 등급이 먼저, 같으면 나중에 받은 것이 먼저.
     * 화면([current])과 진동([vibrating])이 같은 알림을 가리키게 한다(S15P31A101-634): 예전에는 등급만 봐서, 확인하지 않고 기한이 지난
     * '고소'(HIGH) 가 화면에 남은 채 새로 온 '안전모'(CAUTION) 가 진동해 작업자가 무엇 때문에 울리는지 알 수 없었다
     */
    val pending: List<TrackedAlert> =
        items.filter { it.phase != AlertPhase.ACKED }
            .sortedWith(
                compareBy<TrackedAlert> { if (it.phase == AlertPhase.ACTIVE) 0 else 1 }
                    .thenByDescending { it.alert.severity.rank }
                    .thenByDescending { it.receivedAtMs },
            )

    /** 화면에 보이는 알림. 확인하지 않은 것이 없으면 null */
    val current: TrackedAlert? get() = pending.firstOrNull()

    /** 화면에 보이지 않는 확인 대기 알림 수("+N") */
    val others: Int get() = (pending.size - 1).coerceAtLeast(0)

    /** 지금 진동을 내야 하는 알림: 기한 안에 있는 알림 중 가장 급한 것 */
    val vibrating: TrackedAlert? get() = pending.firstOrNull { it.phase == AlertPhase.ACTIVE }

    /** 가장 최근에 확인한 알림. 확인했다는 표시를 보일 때 쓴다 */
    val lastAcked: TrackedAlert? get() = items.filter { it.ackedAtMs != null }.maxByOrNull { it.ackedAtMs ?: 0L }

    /**
     * 알림을 받는다. [nowMs] 는 단조 시계다. 같은 event_id 가 다시 오면:
     * - 확인 대기 중: 내용만 바꾸고 받은 시각(기한의 기준)은 지킨다. 서버가 되풀이해 보내도 기한이 밀리지 않는다
     * - 기한이 지난 뒤: 새로 받은 것으로 센다
     * - 이미 확인함: 같은 알림(alert_msg_id)이 되풀이되면 내용만 바꾸고 다시 울리지 않는다. 다른 알림(새 msg_id: 등급 상향·재발송)이면
     *   새로 받은 것으로 다시 울린다(계약 watch_alert: 확인 뒤 등급이 오른 경보는 그 확인으로 확인된 것이 아니다)
     */
    fun receive(alert: WatchAlert, nowMs: Long): AlertBoard {
        val existing = items.firstOrNull { it.alert.eventId == alert.eventId }
        val tracked = when (existing?.phase) {
            null, AlertPhase.EXPIRED -> TrackedAlert(alert, nowMs)
            AlertPhase.ACTIVE -> existing.copy(alert = alert)
            AlertPhase.ACKED -> if (alert.alertMsgId != existing.alert.alertMsgId) TrackedAlert(alert, nowMs) else existing.copy(alert = alert)
        }
        return AlertBoard(trim(items.filterNot { it.alert.eventId == alert.eventId } + tracked))
    }

    /** 확인한다. 기한이 지난 알림도 받는다. 모르는 알림이거나 이미 확인했으면 그대로다. */
    fun acknowledge(eventId: String, ackedAtMs: Long): AlertBoard {
        val target = items.firstOrNull { it.alert.eventId == eventId && it.phase != AlertPhase.ACKED } ?: return this
        return AlertBoard(items.map { if (it === target) it.copy(ackedAtMs = ackedAtMs) else it })
    }

    /**
     * 이벤트가 해소돼 EC2 가 지우라고 알렸다(I4 watch_alert_clear, S15P31A101-634). 확인 대기 알림(ACTIVE·EXPIRED)을 목록에서 빼 화면·진동에서
     * 사라지게 한다. 확인한 알림은 '확인함' 표시용으로 남긴다. 모르는 이벤트거나 이미 지웠으면 그대로다(같은 통지가 되풀이돼도 된다)
     */
    fun clear(eventId: String): AlertBoard {
        if (items.none { it.alert.eventId == eventId && it.phase != AlertPhase.ACKED }) return this
        return AlertBoard(items.filterNot { it.alert.eventId == eventId && it.phase != AlertPhase.ACKED })
    }

    /** 기한이 지난 확인 대기 알림을 EXPIRED 로 바꾼다. [nowMs] 는 [receive] 와 같은 단조 시계다. */
    fun expireDue(nowMs: Long): AlertBoard {
        if (items.none { it.phase == AlertPhase.ACTIVE && nowMs >= it.deadlineMs }) return this
        return AlertBoard(
            items.map { if (it.phase == AlertPhase.ACTIVE && nowMs >= it.deadlineMs) it.copy(expired = true) else it },
        )
    }

    private fun trim(all: List<TrackedAlert>): List<TrackedAlert> {
        var kept = all
        while (kept.size > MAX_KEPT_ALERTS) {
            val drop = kept.filter { it.phase == AlertPhase.ACKED }.minByOrNull { it.receivedAtMs }
                ?: kept.minByOrNull { it.receivedAtMs }
            kept = kept.filterNot { it === drop }
        }
        return kept
    }
}
