package com.ssafy.simai.watch.ui

import java.time.ZoneOffset
import org.junit.Assert.assertEquals
import org.junit.Test

class ClockTextTest {
    @Test
    fun overdueShowsMinutesAndPaddedSeconds() {
        assertEquals("+0:00", overdueText(0))
        assertEquals("+0:05", overdueText(5_999))
        assertEquals("+0:12", overdueText(12_000))
        assertEquals("+1:00", overdueText(60_000))
        assertEquals("+12:34", overdueText(754_000))
    }

    @Test
    fun overdueNeverShowsANegativeTime() {
        assertEquals("+0:00", overdueText(-5_000))
    }

    @Test
    fun clockUsesTheGivenTimeZone() {
        assertEquals("09:38:51", clockText(1_793_439_531_000L, ZoneOffset.UTC))
    }

    @Test
    fun bigClockDropsSecondsAndFollowsTheHourSetting() {
        val morning = 1_793_439_531_000L // 09:38:51 UTC
        val afternoon = morning + 6 * 3_600_000L // 15:38:51 UTC
        assertEquals("9:38", bigClockText(morning, true, ZoneOffset.UTC))
        assertEquals("9:38", bigClockText(morning, false, ZoneOffset.UTC))
        assertEquals("15:38", bigClockText(afternoon, true, ZoneOffset.UTC))
        assertEquals("3:38", bigClockText(afternoon, false, ZoneOffset.UTC))
    }

    @Test
    fun nextMinuteIsAtMostOneMinuteAway() {
        assertEquals(9_000L, msUntilNextMinute(1_793_439_531_000L)) // 09:38:51 → 9초 뒤
        assertEquals(60_000L, msUntilNextMinute(1_793_439_480_000L)) // 정각이면 한 분 뒤
    }
}
