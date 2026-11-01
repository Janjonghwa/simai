package com.ssafy.simai.watch.alert

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class WatchAlertTest {
    private val base = mapOf(
        AlertFields.EVENT_ID to "e1",
        AlertFields.SEVERITY to "CRITICAL",
        AlertFields.WORD to "멈춤",
        AlertFields.SENT_AT_MS to "1000",
        AlertFields.ACK_WITHIN_MS to "30000",
    )

    private fun fields(vararg changes: Pair<String, String?>): Map<String, String> {
        val result = base.toMutableMap()
        for ((name, value) in changes) if (value == null) result.remove(name) else result[name] = value
        return result
    }

    @Test
    fun readsRequiredFieldsAndDefaultsTheRest() {
        val alert = WatchAlert.fromFields(base)
        assertEquals("e1", alert.eventId)
        assertEquals(AlertSeverity.CRITICAL, alert.severity)
        assertEquals("멈춤", alert.word)
        assertEquals(1000L, alert.sentAtMs)
        assertEquals(30_000L, alert.ackWithinMs)
        assertNull(alert.vibration)
        assertNull(alert.direction)
        assertFalse(alert.needsCheck)
    }

    @Test
    fun readsOptionalFields() {
        val alert = WatchAlert.fromFields(
            fields(
                AlertFields.VIBRATION to "CAUTION_DOUBLE",
                AlertFields.DIRECTION to "LEFT",
                AlertFields.NEEDS_CHECK to "true",
            ),
        )
        assertEquals(VibrationPattern.CAUTION_DOUBLE, alert.vibration)
        assertEquals(AlertDirection.LEFT, alert.direction)
        assertTrue(alert.needsCheck)
    }

    @Test
    fun explicitVibrationBeatsTheSeverityDefault() {
        val plain = WatchAlert.fromFields(base)
        assertEquals(VibrationPattern.DANGER_LONG, plain.pattern)
        val explicit = WatchAlert.fromFields(fields(AlertFields.VIBRATION to "CAUTION_WEAK"))
        assertEquals(VibrationPattern.CAUTION_WEAK, explicit.pattern)
    }

    @Test
    fun severityDecidesThePatternWhenNoneIsGiven() {
        assertEquals(VibrationPattern.DANGER_LONG, VibrationPattern.forSeverity(AlertSeverity.CRITICAL))
        assertEquals(VibrationPattern.CAUTION_DOUBLE, VibrationPattern.forSeverity(AlertSeverity.HIGH))
        assertEquals(VibrationPattern.CAUTION_WEAK, VibrationPattern.forSeverity(AlertSeverity.CAUTION))
    }

    @Test
    fun trimsTheWord() {
        assertEquals("멈춤", WatchAlert.fromFields(fields(AlertFields.WORD to "  멈춤 ")).word)
    }

    @Test
    fun roundTripsThroughFields() {
        val alert = WatchAlert.fromFields(
            fields(
                AlertFields.VIBRATION to "DANGER_LONG",
                AlertFields.DIRECTION to "FRONT",
                AlertFields.NEEDS_CHECK to "true",
            ),
        )
        assertEquals(alert, WatchAlert.fromFields(alert.toFields()))
    }

    @Test
    fun rejectsMissingRequiredFields() {
        for (name in listOf(AlertFields.EVENT_ID, AlertFields.SEVERITY, AlertFields.WORD, AlertFields.SENT_AT_MS, AlertFields.ACK_WITHIN_MS)) {
            assertThrows(name, IllegalArgumentException::class.java) { WatchAlert.fromFields(fields(name to null)) }
            assertThrows(name, IllegalArgumentException::class.java) { WatchAlert.fromFields(fields(name to "  ")) }
        }
    }

    @Test
    fun rejectsBadValues() {
        val bad = listOf(
            AlertFields.SEVERITY to "INFO", // 워치는 위험·경고·주의만 받는다
            AlertFields.SEVERITY to "critical",
            AlertFields.VIBRATION to "BUZZ",
            AlertFields.DIRECTION to "UP",
            AlertFields.NEEDS_CHECK to "yes",
            AlertFields.WORD to "아주아주아주긴단어입니다", // 작은 화면에서 잘린다
            AlertFields.SENT_AT_MS to "0",
            AlertFields.SENT_AT_MS to "abc",
            AlertFields.ACK_WITHIN_MS to "0",
            AlertFields.ACK_WITHIN_MS to "-5",
            AlertFields.ACK_WITHIN_MS to (MAX_ACK_WITHIN_MS + 1).toString(),
        )
        for ((name, value) in bad) {
            assertThrows("$name=$value", IllegalArgumentException::class.java) { WatchAlert.fromFields(fields(name to value)) }
        }
    }

    @Test
    fun acceptsTheLimits() {
        WatchAlert.fromFields(fields(AlertFields.ACK_WITHIN_MS to "1"))
        WatchAlert.fromFields(fields(AlertFields.ACK_WITHIN_MS to MAX_ACK_WITHIN_MS.toString()))
        WatchAlert.fromFields(fields(AlertFields.WORD to "가".repeat(MAX_WORD_LENGTH)))
    }

    @Test
    fun patternsAreWellFormed() {
        for (pattern in VibrationPattern.entries) {
            assertEquals(pattern.name, pattern.timingsMs.size, pattern.amplitudes.size)
            assertEquals(pattern.name, 0L, pattern.timingsMs.first()) // 첫 칸은 시작 지연
            val repeatFrom = pattern.repeatFrom
            assertTrue(pattern.name, repeatFrom == null || repeatFrom in 1 until pattern.timingsMs.size)
        }
        // 위험만 확인될 때까지 되풀이한다
        assertEquals(listOf(VibrationPattern.DANGER_LONG), VibrationPattern.entries.filter { it.repeatFrom != null })
    }
}
