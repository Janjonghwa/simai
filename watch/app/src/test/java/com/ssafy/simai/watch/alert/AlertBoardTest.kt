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
    fun screenAndVibrationShowTheSameStillActiveAlert() {
        // -634: 화면은 지금 울리는(기한 안) 알림을 먼저 보인다. 예전에는 기한 지난 더 급한 알림이 화면에 남아 진동과 글자가 달랐다
        val board = AlertBoard()
            .receive(alert("critical", AlertSeverity.CRITICAL, ackWithinMs = 1_000), 1_000)
            .receive(alert("high", AlertSeverity.HIGH, ackWithinMs = 60_000), 1_000)
            .expireDue(5_000)
        assertEquals("high", board.current?.alert?.eventId)
        assertEquals("high", board.vibrating?.alert?.eventId)
        assertEquals(1, board.others)
        // 울리는 알림을 확인하면 기한 지난 위험이 다시 앞에 온다(확인 단추가 그것을 확인한다)
        val acked = board.acknowledge("high", 6_000)
        assertEquals("critical", acked.current?.alert?.eventId)
        assertNull(acked.vibrating)
    }

    @Test
    fun newHelmetCautionIsShownOverAnExpiredUnconfirmedHeightAlert() {
        // 시연 장면 2→3: '고소'(HIGH)를 확인하지 않아 기한이 지났는데 '안전모'(CAUTION)가 오면 화면·진동 모두 '안전모'다
        val board = AlertBoard()
            .receive(alert("r3", AlertSeverity.HIGH, ackWithinMs = 10_000, word = "고소"), 1_000)
            .expireDue(20_000)
            .receive(alert("r9", AlertSeverity.CAUTION, ackWithinMs = 10_000, word = "안전모"), 21_000)
        assertEquals("안전모", board.current?.alert?.word)
        assertEquals("안전모", board.vibrating?.alert?.word)
        // 둘 다 기한이 지나면 더 급한 쪽이 앞이다
        assertEquals("고소", board.expireDue(40_000).current?.alert?.word)
    }

    @Test
    fun clearRemovesAPendingAlertAndStopsItsVibration() {
        val board = AlertBoard()
            .receive(alert("r9", AlertSeverity.CAUTION, word = "안전모"), 1_000)
            .receive(alert("r11", AlertSeverity.CAUTION, word = "조끼"), 2_000)
        val cleared = board.clear("r11")
        assertEquals("안전모", cleared.current?.alert?.word)
        assertEquals("안전모", cleared.vibrating?.alert?.word)
        assertEquals(0, cleared.others)
        val empty = cleared.clear("r9")
        assertNull(empty.current)
        assertNull(empty.vibrating)
    }

    @Test
    fun clearAlsoRemovesAnExpiredAlertButKeepsAcknowledgedOnesAndIgnoresUnknown() {
        val board = AlertBoard()
            .receive(alert("expired", ackWithinMs = 1_000), 1_000)
            .receive(alert("acked"), 1_000)
            .acknowledge("acked", 2_000)
            .expireDue(5_000)
        val cleared = board.clear("expired")
        assertNull(cleared.current)
        assertSame(cleared, cleared.clear("acked"))
        assertEquals("acked", cleared.lastAcked?.alert?.eventId)
        assertSame(cleared, cleared.clear("nope"))
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
