package com.ssafy.simai.watch.ui

import kotlin.math.abs
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AmbientStateTest {
    private val start = 1_793_439_531_000L // 09:38:51 UTC

    @Test
    fun shiftMovesEveryMinuteAndRepeatsEveryFourMinutes() {
        val shifts = (0 until 4).map { burnInShiftDp(start + it * 60_000L) }
        assertEquals(4, shifts.toSet().size)
        assertEquals(shifts[0], burnInShiftDp(start + 4 * 60_000L))
    }

    @Test
    fun shiftStaysTheSameInsideOneMinute() {
        assertEquals(burnInShiftDp(start), burnInShiftDp(start + 5_000L)) // 09:38:51 → 09:38:56
    }

    @Test
    fun shiftStaysWithinThreeDp() {
        (0 until 8).map { burnInShiftDp(start + it * 60_000L) }.forEach { (x, y) ->
            assertTrue(abs(x) <= 3 && abs(y) <= 3)
        }
    }
}
