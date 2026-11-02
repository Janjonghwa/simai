package com.ssafy.simai.watch.alert

/**
 * 알림 값의 칸 이름. I4 `watch_alert` 계약(contracts/messages/watch_alert.schema.json)의 payload 필드 이름 그대로이고,
 * 봉투에서 오는 둘(`msg_id` → [ALERT_MSG_ID], `sent_at` → [SENT_AT])만 이름을 바꿔 싣는다. 인텐트 extra 도 같은 이름을 쓴다.
 */
object AlertFields {
    /** 봉투 msg_id. 알림 ID 이고 watch_ack.alert_msg_id 로 돌려준다 */
    const val ALERT_MSG_ID = "alert_msg_id"

    /** 봉투 sent_at(EC2 시계, UTC ms) */
    const val SENT_AT = "sent_at"
    const val EVENT_ID = "event_id"
    const val EVENT_SEQUENCE = "event_sequence"
    const val SEVERITY = "severity"
    const val VIBRATION_PATTERN = "vibration_pattern"
    const val DIRECTION = "direction"
    const val WORD = "word"
    const val ACK_WITHIN_MS = "ack_within_ms"
    const val NEEDS_CHECK = "needs_check"

    /** payload 안의 칸(봉투에서 온 둘을 뺀 것) */
    val PAYLOAD = listOf(EVENT_ID, EVENT_SEQUENCE, SEVERITY, VIBRATION_PATTERN, DIRECTION, WORD, ACK_WITHIN_MS, NEEDS_CHECK)
    val ALL = listOf(ALERT_MSG_ID, SENT_AT) + PAYLOAD
}

/** 위험 쪽을 가리키는 방향, 작업자 기준(Tier 0 트랙 velocity 의 진행 방향이 FRONT, 계약 watch_alert.direction). */
enum class AlertDirection(val arrow: String) {
    FRONT("↑"),
    BACK("↓"),
    LEFT("←"),
    RIGHT("→"),
}

/** 한 단어가 길면 작은 화면에서 잘린다(계약 maxLength 8) */
const val MAX_WORD_LENGTH = 8

/** 확인 기한의 상한(계약 maximum 10분). 이보다 길면 값이 잘못된 것으로 본다 */
const val MAX_ACK_WITHIN_MS = 10 * 60 * 1000L

/**
 * EC2 가 워치로 보내는 위험 알림 한 건(I4 watch_alert).
 *
 * @property alertMsgId 봉투 msg_id. 같은 값이 다시 오면 진동하지 않고 ACK 만 다시 보낸다
 * @property eventSequence 알림을 만들 때의 이벤트 상태. ACK 에 그대로 돌려준다. 없을 수 있다
 * @property vibration 서버가 정한 패턴. 없으면 등급으로 정한다
 * @property sentAtMs EC2 가 보낸 시각(봉투 sent_at, EC2 시계). 화면에는 쓰지 않고 지연을 재는 데 쓴다
 * @property ackWithinMs 받은 때부터 이 시간 안에 ACK 가 EC2 에 닿지 않으면 EC2 가 현장 경보·관리자 알림으로 격상한다.
 *   시계 차이에 흔들리지 않게 절대 시각이 아니라 길이로 받는다
 */
data class WatchAlert(
    val alertMsgId: String,
    val eventId: String,
    val eventSequence: Long?,
    val severity: AlertSeverity,
    val vibration: VibrationPattern?,
    val direction: AlertDirection?,
    val word: String,
    val sentAtMs: Long,
    val ackWithinMs: Long,
    val needsCheck: Boolean,
) {
    val pattern: VibrationPattern get() = vibration ?: VibrationPattern.forSeverity(severity)

    /** [fromFields] 의 역. 인텐트로 알림을 넘길 때 쓴다. */
    fun toFields(): Map<String, String> = buildMap {
        put(AlertFields.ALERT_MSG_ID, alertMsgId)
        put(AlertFields.SENT_AT, sentAtMs.toString())
        put(AlertFields.EVENT_ID, eventId)
        eventSequence?.let { put(AlertFields.EVENT_SEQUENCE, it.toString()) }
        put(AlertFields.SEVERITY, severity.name)
        vibration?.let { put(AlertFields.VIBRATION_PATTERN, it.name) }
        direction?.let { put(AlertFields.DIRECTION, it.name) }
        put(AlertFields.WORD, word)
        put(AlertFields.ACK_WITHIN_MS, ackWithinMs.toString())
        put(AlertFields.NEEDS_CHECK, needsCheck.toString())
    }

    companion object {
        /** 잘못된 값이면 IllegalArgumentException. 알림 하나가 잘못돼도 앱이 죽지 않게 부르는 쪽이 받아 버린다. */
        fun fromFields(fields: Map<String, String>): WatchAlert {
            val word = required(fields, AlertFields.WORD).trim()
            require(word.isNotEmpty() && word.length <= MAX_WORD_LENGTH) {
                "${AlertFields.WORD} 는 1~${MAX_WORD_LENGTH}자여야 한다"
            }
            val ackWithinMs = requiredLong(fields, AlertFields.ACK_WITHIN_MS)
            require(ackWithinMs in 1..MAX_ACK_WITHIN_MS) { "${AlertFields.ACK_WITHIN_MS} 는 1~${MAX_ACK_WITHIN_MS} 여야 한다" }
            val sentAtMs = requiredLong(fields, AlertFields.SENT_AT)
            require(sentAtMs > 0) { "${AlertFields.SENT_AT} 는 0 보다 커야 한다" }
            val eventSequence = fields[AlertFields.EVENT_SEQUENCE]?.let {
                it.toLongOrNull() ?: throw IllegalArgumentException("${AlertFields.EVENT_SEQUENCE} 는 정수여야 한다")
            }
            require(eventSequence == null || eventSequence >= 1) { "${AlertFields.EVENT_SEQUENCE} 는 1 이상이어야 한다" }
            return WatchAlert(
                alertMsgId = required(fields, AlertFields.ALERT_MSG_ID),
                eventId = required(fields, AlertFields.EVENT_ID),
                eventSequence = eventSequence,
                severity = enumOf<AlertSeverity>(required(fields, AlertFields.SEVERITY), AlertFields.SEVERITY),
                vibration = fields[AlertFields.VIBRATION_PATTERN]?.let { enumOf<VibrationPattern>(it, AlertFields.VIBRATION_PATTERN) },
                direction = fields[AlertFields.DIRECTION]?.let { enumOf<AlertDirection>(it, AlertFields.DIRECTION) },
                word = word,
                sentAtMs = sentAtMs,
                ackWithinMs = ackWithinMs,
                needsCheck = fields[AlertFields.NEEDS_CHECK]?.let(::booleanOf) ?: false,
            )
        }

        private fun required(fields: Map<String, String>, name: String): String {
            val value = fields[name]
            require(!value.isNullOrBlank()) { "$name 가 비었다" }
            return value
        }

        private fun requiredLong(fields: Map<String, String>, name: String): Long =
            required(fields, name).toLongOrNull() ?: throw IllegalArgumentException("$name 는 정수여야 한다")

        private inline fun <reified E : Enum<E>> enumOf(raw: String, name: String): E =
            enumValues<E>().firstOrNull { it.name == raw } ?: throw IllegalArgumentException("$name 값이 잘못됐다")

        private fun booleanOf(raw: String): Boolean = when (raw) {
            "true" -> true
            "false" -> false
            else -> throw IllegalArgumentException("${AlertFields.NEEDS_CHECK} 는 true 또는 false 여야 한다")
        }
    }
}
