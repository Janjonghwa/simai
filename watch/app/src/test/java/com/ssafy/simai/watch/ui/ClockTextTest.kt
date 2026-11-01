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
}
