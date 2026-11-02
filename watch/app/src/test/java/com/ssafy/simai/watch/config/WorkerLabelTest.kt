package com.ssafy.simai.watch.config

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class WorkerLabelTest {
    @Test
    fun keepsNameAndZone() {
        val label = WorkerLabel("김반장", "3층 A구역")
        assertEquals("김반장", label.name)
        assertEquals("3층 A구역", label.zone)
    }

    @Test
    fun rejectsBlankNameOrZone() {
        assertThrows(IllegalArgumentException::class.java) { WorkerLabel("", "3층 A구역") }
        assertThrows(IllegalArgumentException::class.java) { WorkerLabel("김반장", "  ") }
    }

    @Test
    fun rejectsTextTooLongForOneLine() {
        val tooLong = "가".repeat(WorkerLabel.MAX_LENGTH + 1)
        assertThrows(IllegalArgumentException::class.java) { WorkerLabel(tooLong, "3층 A구역") }
        assertThrows(IllegalArgumentException::class.java) { WorkerLabel("김반장", tooLong) }
        WorkerLabel("가".repeat(WorkerLabel.MAX_LENGTH), "나".repeat(WorkerLabel.MAX_LENGTH))
    }
}
