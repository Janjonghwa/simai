package com.ssafy.simai.watch.alert

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

class AlertBoardTest {
    private fun alert(
        eventId: String,
        severity: AlertSeverity = AlertSeverity.CRITICAL,
        ackWithinMs: Long = 30_000L,
        word: String = "멈춤",
        msgId: String = "msg-$eventId",
    ) = WatchAlert(msgId, eventId, null, severity, null, null, word, sentAtMs = 1L, ackWithinMs = ackWithinMs, needsCheck = false)

    @Test
    fun newAlertForAnAckedEventRingsAgainButARepeatDoesNot() {
        val acked = AlertBoard().receive(alert("a"), 1_000).acknowledge("a", 5_000)
        assertEquals(AlertPhase.ACKED, acked.items.single().phase)
        val repeated = acked.receive(alert("a", word = "다시"), 6_000)
        assertEquals(AlertPhase.ACKED, repeated.items.single().phase)
        assertEquals("다시", repeated.items.single().alert.word)
        val escalated = acked.receive(alert("a", msgId = "msg-a-2"), 7_000)
        assertEquals(AlertPhase.ACTIVE, escalated.items.single().phase)
        assertEquals(7_000L, escalated.items.single().receivedAtMs)
        assertEquals("a", escalated.vibrating?.alert?.eventId)
    }

    @Test
    fun emptyBoardShowsNothing() {
        val board = AlertBoard()
        assertNull(board.current)
        assertNull(board.vibrating)
        assertNull(board.lastAcked)
        assertEquals(0, board.others)
    }

    @Test
    fun receivedAlertIsActiveAndVibrates() {
        val board = AlertBoard().receive(alert("a"), nowMs = 1_000)
        assertEquals("a", board.current?.alert?.eventId)
        assertEquals(AlertPhase.ACTIVE, board.current?.phase)
        assertEquals("a", board.vibrating?.alert?.eventId)
    }

    @Test
    fun moreSevereAlertComesFirstAndOthersAreCounted() {
        val board = AlertBoard()
            .receive(alert("caution", AlertSeverity.CAUTION), 1_000)
            .receive(alert("critical", AlertSeverity.CRITICAL), 2_000)
            .receive(alert("high", AlertSeverity.HIGH), 3_000)
        assertEquals("critical", board.current?.alert?.eventId)
        assertEquals(2, board.others)
        assertEquals("critical", board.vibrating?.alert?.eventId)
    }

    @Test
    fun sameSeverityShowsTheLaterOneFirst() {
        val board = AlertBoard().receive(alert("old"), 1_000).receive(alert("new"), 2_000)
        assertEquals("new", board.current?.alert?.eventId)
    }

    @Test
    fun acknowledgingShowsTheNextAlertAndKeepsTheAckTime() {
        val board = AlertBoard()
            .receive(alert("critical", AlertSeverity.CRITICAL), 1_000)
            .receive(alert("high", AlertSeverity.HIGH), 2_000)
            .acknowledge("critical", ackedAtMs = 5_000)
        assertEquals("high", board.current?.alert?.eventId)
        assertEquals(0, board.others)
        assertEquals("critical", board.lastAcked?.alert?.eventId)
        assertEquals(5_000L, board.lastAcked?.ackedAtMs)
    }

    @Test
    fun acknowledgingTheLastAlertLeavesNothingPending() {
        val board = AlertBoard().receive(alert("a"), 1_000).acknowledge("a", 2_000)
        assertNull(board.current)
        assertNull(board.vibrating)
        assertEquals(2_000L, board.lastAcked?.ackedAtMs)
    }

    @Test
    fun acknowledgingAnUnknownOrAlreadyAckedAlertChangesNothing() {
        val board = AlertBoard().receive(alert("a"), 1_000)
        assertSame(board, board.acknowledge("zzz", 2_000))
        val acked = board.acknowledge("a", 2_000)
        assertSame(acked, acked.acknowledge("a", 9_000))
        assertEquals(2_000L, acked.lastAcked?.ackedAtMs)
    }

    @Test
    fun deadlinePassingStopsTheVibrationButKeepsTheAlertOnScreen() {
        val board = AlertBoard().receive(alert("a", ackWithinMs = 10_000), 1_000)
        assertSame(board, board.expireDue(10_999))
        val expired = board.expireDue(11_000)
        assertEquals(AlertPhase.EXPIRED, expired.current?.phase)
        assertNull(expired.vibrating)
    }

    @Test
    fun expiredAlertCanStillBeAcknowledged() {
        val acked = AlertBoard().receive(alert("a", ackWithinMs = 1_000), 1_000).expireDue(5_000).acknowledge("a", 6_000)
        assertNull(acked.current)
        assertEquals(6_000L, acked.lastAcked?.ackedAtMs)
    }

    @Test
    fun vibrationFallsToTheMostSevereAlertThatIsStillActive() {
        val board = AlertBoard()
            .receive(alert("critical", AlertSeverity.CRITICAL, ackWithinMs = 1_000), 1_000)
            .receive(alert("high", AlertSeverity.HIGH, ackWithinMs = 60_000), 1_000)
            .expireDue(5_000)
        // 화면에는 더 급한 위험이 남고, 진동은 아직 기한 안인 경고가 낸다
        assertEquals("critical", board.current?.alert?.eventId)
        assertEquals("high", board.vibrating?.alert?.eventId)
    }

    @Test
    fun repeatedActiveAlertKeepsItsReceiveTimeSoTheDeadlineDoesNotSlide() {
        val first = alert("a", ackWithinMs = 10_000, word = "멈춤")
        val board = AlertBoard().receive(first, 1_000).receive(first.copy(word = "물러남"), 8_000)
        assertEquals("물러남", board.current?.alert?.word)
        assertEquals(1_000L, board.current?.receivedAtMs)
        assertEquals(AlertPhase.EXPIRED, board.expireDue(11_000).current?.phase)
    }

    @Test
    fun repeatedExpiredAlertStartsOverAsActive() {
        val first = alert("a", ackWithinMs = 1_000)
        val board = AlertBoard().receive(first, 1_000).expireDue(5_000).receive(first, 6_000)
        assertEquals(AlertPhase.ACTIVE, board.current?.phase)
        assertEquals(6_000L, board.current?.receivedAtMs)
    }

    @Test
    fun repeatedAckedAlertDoesNotRingAgain() {
        val first = alert("a")
        val board = AlertBoard().receive(first, 1_000).acknowledge("a", 2_000).receive(first.copy(word = "물러남"), 3_000)
        assertNull(board.current)
        assertNull(board.vibrating)
        assertEquals("물러남", board.lastAcked?.alert?.word)
    }

    @Test
    fun remainingFractionShrinksFromOneToZeroAndStaysAtZero() {
        val tracked = AlertBoard().receive(alert("a", ackWithinMs = 10_000), 1_000).current!!
        assertEquals(1f, tracked.remainingFraction(1_000), 0.0001f)
        assertEquals(0.5f, tracked.remainingFraction(6_000), 0.0001f)
        assertEquals(0f, tracked.remainingFraction(11_000), 0.0001f)
        assertEquals(0f, tracked.remainingFraction(99_000), 0.0001f)
        assertEquals(1f, tracked.remainingFraction(0), 0.0001f) // 시계가 거꾸로 가도 1 을 넘지 않는다
    }

    @Test
    fun overdueIsZeroBeforeTheDeadlineAndGrowsAfterIt() {
        val tracked = AlertBoard().receive(alert("a", ackWithinMs = 10_000), 1_000).current!!
        assertEquals(0L, tracked.overdueMs(10_999))
        assertEquals(0L, tracked.overdueMs(11_000))
        assertEquals(12_000L, tracked.overdueMs(23_000))
    }

    @Test
    fun keepsAtMostTheLimitDroppingAckedFirstThenOldest() {
        var board = AlertBoard()
        for (i in 1..MAX_KEPT_ALERTS) board = board.receive(alert("e$i"), i.toLong())
        board = board.acknowledge("e5", 100)
        board = board.receive(alert("extra"), 200)
        assertEquals(MAX_KEPT_ALERTS, board.items.size)
        assertEquals(false, board.items.any { it.alert.eventId == "e5" }) // 확인한 것이 먼저 버려진다
        board = board.receive(alert("extra2"), 300)
        assertEquals(MAX_KEPT_ALERTS, board.items.size)
        assertEquals(false, board.items.any { it.alert.eventId == "e1" }) // 그다음 가장 오래된 것
    }
}
