package com.ssafy.simai.watch.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class WordSizeTest {
    @Test
    fun shortWordsGetTheLargestSize() {
        assertEquals(36f, wordFontSizeSp(227f, 1), 0.001f)
        assertEquals(36f, wordFontSizeSp(227f, 4), 0.001f)
    }

    @Test
    fun longWordsShrinkToFitTheScreenWidth() {
        assertEquals(227f * 0.7f / 5, wordFontSizeSp(227f, 5), 0.001f)
        assertTrue(wordFontSizeSp(227f, 6) < wordFontSizeSp(227f, 5))
    }

    @Test
    fun neverGoesBelowTheMinimumEvenForTheLongestWordOnTheSmallestScreen() {
        assertEquals(20f, wordFontSizeSp(192f, 8), 0.001f)
    }

    @Test
    fun smallerScreensGetSmallerOrEqualSizes() {
        for (length in 1..8) assertTrue(wordFontSizeSp(192f, length) <= wordFontSizeSp(227f, length))
    }

    @Test
    fun smallScreensCapTheLargestSizeByWidth() {
        assertEquals(192f * 0.16f, wordFontSizeSp(192f, 1), 0.001f)
        assertEquals(36f, wordFontSizeSp(227f, 1), 0.001f)
    }

    @Test
    fun anEmptyWordDoesNotDivideByZero() {
        assertEquals(36f, wordFontSizeSp(227f, 0), 0.001f)
    }
}
