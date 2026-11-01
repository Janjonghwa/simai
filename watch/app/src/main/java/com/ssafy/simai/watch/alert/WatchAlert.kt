package com.ssafy.simai.watch.alert

/** 알림 값의 칸 이름. 인텐트 extra 와 I4 메시지가 같은 이름을 쓴다(필드 이름은 S15P31A101-334 계약이 정해지면 맞춘다). */
object AlertFields {
    const val EVENT_ID = "event_id"
    const val SEVERITY = "severity"
    const val VIBRATION = "vibration"
    const val DIRECTION = "direction"
    const val WORD = "word"
    const val SENT_AT_MS = "sent_at_ms"
    const val ACK_WITHIN_MS = "ack_within_ms"
    const val NEEDS_CHECK = "needs_check"

    val ALL = listOf(EVENT_ID, SEVERITY, VIBRATION, DIRECTION, WORD, SENT_AT_MS, ACK_WITHIN_MS, NEEDS_CHECK)
}

/** 위험 쪽을 가리키는 방향(작업자 기준). 기준은 계약에서 정한다. */
enum class AlertDirection(val arrow: String) {
    FRONT("↑"),
    BACK("↓"),
    LEFT("←"),
    RIGHT("→"),
}

/** 한 단어가 길면 작은 화면에서 잘린다 */
const val MAX_WORD_LENGTH = 8

/** 확인 기한의 상한. 이보다 길면 값이 잘못된 것으로 본다 */
const val MAX_ACK_WITHIN_MS = 10 * 60 * 1000L

/**
 * EC2 가 워치로 보내는 위험 알림 한 건(watch_alert).
 *
 * @property vibration 서버가 정한 패턴. 없으면 등급으로 정한다
 * @property sentAtMs EC2 가 보낸 시각(UTC ms). 화면에는 쓰지 않고 지연을 재는 데 쓴다
 * @property ackWithinMs 받은 때부터 이 시간 안에 확인하지 않으면 EC2 가 현장 경보·관리자 알림으로 격상한다.
 *   시계 차이에 흔들리지 않게 절대 시각이 아니라 길이로 받는다
 */
data class WatchAlert(
    val eventId: String,
    val severity: AlertSeverity,
    val vibration: VibrationPattern?,
    val direction: AlertDirection?,
    val word: String,
    val sentAtMs: Long,
    val ackWithinMs: Long,
    val needsCheck: Boolean,
) {
    val pattern: VibrationPattern get() = vibration ?: VibrationPattern.forSeverity(severity)

    /** [fromFields] 의 역. 인텐트로 알림 화면을 띄울 때 쓴다. */
    fun toFields(): Map<String, String> = buildMap {
        put(AlertFields.EVENT_ID, eventId)
        put(AlertFields.SEVERITY, severity.name)
        vibration?.let { put(AlertFields.VIBRATION, it.name) }
        direction?.let { put(AlertFields.DIRECTION, it.name) }
        put(AlertFields.WORD, word)
        put(AlertFields.SENT_AT_MS, sentAtMs.toString())
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
            val sentAtMs = requiredLong(fields, AlertFields.SENT_AT_MS)
            require(sentAtMs > 0) { "${AlertFields.SENT_AT_MS} 는 0 보다 커야 한다" }
            return WatchAlert(
                eventId = required(fields, AlertFields.EVENT_ID),
                severity = enumOf<AlertSeverity>(required(fields, AlertFields.SEVERITY), AlertFields.SEVERITY),
                vibration = fields[AlertFields.VIBRATION]?.let { enumOf<VibrationPattern>(it, AlertFields.VIBRATION) },
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
