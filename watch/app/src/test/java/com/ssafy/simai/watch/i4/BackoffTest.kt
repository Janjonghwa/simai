package com.ssafy.simai.watch.i4

import org.junit.Assert.assertEquals
import org.junit.Test

class BackoffTest {
    @Test
    fun doublesUpToTheCapAndResets() {
        val backoff = Backoff()
        assertEquals(listOf(1_000L, 2_000L, 4_000L, 8_000L, 16_000L, 30_000L, 30_000L), List(7) { backoff.next() })
        backoff.reset()
        assertEquals(1_000L, backoff.next())
    }
}
