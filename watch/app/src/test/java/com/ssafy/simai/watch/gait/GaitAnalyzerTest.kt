package com.ssafy.simai.watch.gait

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.sin

/** 합성 손목 가속도로 걸음 특징을 본다: 걷기는 1.8 Hz 로 2 m/s² 흔들림, 서 있음은 작은 떨림 */
class GaitAnalyzerTest {
    private val gravity = 9.81
    private val stepHz = 1.8

    private fun feed(analyzer: GaitAnalyzer, fromMs: Long, toMs: Long, walking: Boolean) {
        var t = fromMs
        while (t < toMs) {
            val s = t / 1000.0
            val az = if (walking) gravity + 2.0 * sin(2 * PI * stepHz * s) else gravity + 0.02 * sin(2 * PI * 7 * s)
            analyzer.add(t, 0.0, 0.0, az)
            t += 1000L / SAMPLE_RATE_HZ
        }
    }

    @Test
    fun walkingGivesStepsPeakFrequencyAndAStart() {
        val analyzer = GaitAnalyzer()
        feed(analyzer, 0, 6_000, walking = true)
        // 첫 창은 4초가 차야 나온다(90% 채움). 전환은 창의 새 절반(2~4초)에서 판정한다
        assertNull(analyzer.window(2_000))
        val first = analyzer.window(4_000)!!
        assertEquals(listOf(MotionKind.START), first.transitions.map { it.kind })
        assertTrue(first.transitions.single().atMs in 2_000..3_500)
        val window = analyzer.window(6_000)!!
        assertEquals(2_000L, window.windowStartMs)
        assertEquals(6_000L, window.windowEndMs)
        assertEquals(SAMPLE_RATE_HZ, window.sampleRateHz)
        assertTrue("걸음 ${window.stepTimesMs.size}", window.stepTimesMs.size in 6..8)
        assertTrue(window.stepTimesMs.zipWithNext().all { (a, b) -> b - a >= MIN_STEP_INTERVAL_MS })
        assertTrue(window.stepTimesMs.all { it in 2_000 until 6_000 })
        assertNotNull(window.peakFreqHz)
        assertTrue("peak ${window.peakFreqHz}", window.peakFreqHz!! in 1.7..1.9)
        assertTrue("rms ${window.rms}", window.rms > 0.5)
        assertEquals(40, window.bandValues.size)
        assertEquals(BAND_RATE_HZ, window.bandRateHz)
        assertTrue(window.transitions.isEmpty()) // 이미 걷는 중: 새 전환 없음
        assertTrue(window.moving && analyzer.moving)
        assertTrue(window.bandValues.all { it.isFinite() })
    }

    @Test
    fun standingStillGivesNothing() {
        val analyzer = GaitAnalyzer()
        feed(analyzer, 0, 6_000, walking = false)
        analyzer.window(4_000)
        val window = analyzer.window(6_000)!!
        assertTrue(window.stepTimesMs.isEmpty())
        assertNull(window.peakFreqHz)
        assertTrue(window.transitions.isEmpty())
        assertTrue("rms ${window.rms}", window.rms < 0.1)
        assertTrue(!window.moving && !analyzer.moving)
    }

    @Test
    fun stoppingAfterWalkingGivesOneStopInTheNewHalfOnly() {
        val analyzer = GaitAnalyzer()
        feed(analyzer, 0, 6_000, walking = true)
        analyzer.window(4_000)
        analyzer.window(6_000)
        feed(analyzer, 6_000, 12_000, walking = false)
        val stopping = analyzer.window(8_000)!!
        assertEquals(listOf(MotionKind.STOP), stopping.transitions.map { it.kind })
        assertTrue(stopping.transitions.single().atMs in 6_000..7_000)
        assertTrue(!analyzer.moving)
        val still = analyzer.window(10_000)!!
        assertTrue(still.transitions.isEmpty())
        assertTrue(still.stepTimesMs.isEmpty())
    }

    @Test
    fun aHalfSecondPauseWhileWalkingDoesNotStop() {
        val analyzer = GaitAnalyzer()
        feed(analyzer, 0, 4_000, walking = true)
        analyzer.window(4_000)
        feed(analyzer, 4_000, 4_500, walking = false) // 팔이 반 박자 멈춤
        feed(analyzer, 4_500, 8_000, walking = true)
        val window = analyzer.window(6_000)!!
        assertTrue("전환 ${window.transitions}", window.transitions.isEmpty())
        assertTrue(analyzer.moving)
        assertTrue(analyzer.window(8_000)!!.transitions.isEmpty())
    }

    @Test
    fun windowNeedsAlmostAllTheSamples() {
        val analyzer = GaitAnalyzer()
        feed(analyzer, 0, 3_000, walking = true)
        assertNull(analyzer.window(4_000)) // 75% 는 모자란다(빈 칸을 0 으로 채우면 없는 정지가 된다)
        feed(analyzer, 3_000, 3_700, walking = true)
        assertNotNull(analyzer.window(4_000))
        assertEquals(3_680L, analyzer.latestSampleMs)
    }

    @Test
    fun badSamplesAreIgnoredAndAGapResetsTheFilter() {
        val analyzer = GaitAnalyzer()
        feed(analyzer, 0, 2_000, walking = true)
        analyzer.add(2_000, Double.NaN, 0.0, 9.81) // 센서 글리치
        analyzer.add(1_000, 0.0, 0.0, 9.81) // 시계 역행
        assertEquals(1_980L, analyzer.latestSampleMs)
        // 12초 공백(CPU 수면) 뒤 서 있음: 공백 양끝의 차이가 걸음·시작으로 보이면 안 된다
        feed(analyzer, 14_000, 20_000, walking = false)
        val window = analyzer.window(20_000)!!
        assertTrue(window.stepTimesMs.isEmpty())
        assertTrue(window.transitions.isEmpty())
        assertTrue("rms ${window.rms}", window.rms < 0.1)
        assertTrue(window.bandValues.none { it.isNaN() })
    }

    @Test
    fun resetForgetsEverything() {
        val analyzer = GaitAnalyzer()
        feed(analyzer, 0, 6_000, walking = true)
        analyzer.window(4_000)
        assertTrue(analyzer.moving)
        analyzer.reset()
        assertTrue(!analyzer.moving)
        assertNull(analyzer.latestSampleMs)
        assertNull(analyzer.window(6_000))
    }

    @Test
    fun oldSamplesAreDroppedButABurstIsKept() {
        val analyzer = GaitAnalyzer()
        feed(analyzer, 0, 60_000, walking = true)
        assertNull(analyzer.window(20_000)) // 보관 길이(30초)보다 오래된 샘플은 버렸다
        assertNotNull(analyzer.window(34_000)) // 한꺼번에 온 묶음은 창 단위로 되짚을 수 있다
        assertNotNull(analyzer.window(60_000))
    }
}
