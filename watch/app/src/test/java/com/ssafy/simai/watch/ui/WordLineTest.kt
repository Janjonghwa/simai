package com.ssafy.simai.watch.ui

import com.ssafy.simai.watch.alert.AlertDirection
import org.junit.Assert.assertEquals
import org.junit.Test

class WordLineTest {
    @Test
    fun withoutADirectionTheLineIsJustTheWord() {
        assertEquals("멈춤", wordLine("멈춤", null))
    }

    @Test
    fun rightPutsTheArrowAfterTheWord() {
        assertEquals("멈춤 →", wordLine("멈춤", AlertDirection.RIGHT))
    }

    @Test
    fun otherDirectionsPutTheArrowBeforeTheWord() {
        assertEquals("← 멈춤", wordLine("멈춤", AlertDirection.LEFT))
        assertEquals("↑ 멈춤", wordLine("멈춤", AlertDirection.FRONT))
        assertEquals("↓ 멈춤", wordLine("멈춤", AlertDirection.BACK))
    }
}
