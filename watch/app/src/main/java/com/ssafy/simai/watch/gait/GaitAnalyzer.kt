package com.ssafy.simai.watch.gait

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/** 걸음으로 세는 대역 통과 봉우리의 최소 크기(m/s²). 서 있을 때 손떨림(0.1 안팎)보다 크고 느린 걸음(0.7 안팎)보다 작게 */
const val STEP_PEAK_MIN = 0.5

/** 걸음 사이 최소 간격(ms). 초당 3.3 걸음 넘게는 걸음이 아니다(뛰기도 2.5~3 걸음/초) */
const val MIN_STEP_INTERVAL_MS = 300L

/** 걷기 시작·멈춤 판정의 RMS 문턱(히스테리시스, m/s²)과 블록 길이. 멈춤은 조용한 블록이 이만큼 이어져야 한다(걷는 중 팔이 잠깐 멈추는 반 박자에 깜빡이지 않게) */
const val MOVING_RMS_ON = 0.6
const val MOVING_RMS_OFF = 0.35
const val MOTION_BLOCK_MS = 500L
const val STILL_BLOCKS_TO_STOP = 2

/** 뚜렷한 주파수 성분: 최대 성분이 대역 평균의 이 배수 이상이고 움직임이 있을 때 */
const val PEAK_PROMINENCE = 4.0
const val PEAK_FREQ_STEP_HZ = 0.05

/** 창이 이 비율 이상 샘플로 차야 특징을 낸다. 비는 칸을 0 으로 채우면 없는 정지를 만들어 내므로 거의 다 찼을 때만 낸다 */
const val MIN_WINDOW_FILL = 0.9

/** 샘플 간격이 이만큼(샘플 주기의 5배) 넘게 비면 공백으로 보고 필터를 비운다 */
const val GAP_SAMPLE_PERIODS = 5

/**
 * 샘플을 들고 있는 길이. CPU 가 잠든 동안 센서 FIFO 에 쌓였다 한꺼번에 오는 묶음(실기기에서 10~15초)을 창 단위로 다 처리하려면
 * 창 하나보다 훨씬 길어야 한다. 50 Hz 30초 = 1,500 샘플
 */
const val RETAIN_MS = 30_000L

/**
 * 손목 가속도에서 걸음 특징을 뽑는다(계약 gait_features, 부록 C.6 움직임 대조의 워치 쪽 입력). 시계를 읽지 않고 샘플 시각을 받는
 * 순수한 값이라 JVM 에서 합성 신호로 시험한다.
 *
 * - 입력: 3축 가속도(m/s², 중력 포함)의 크기 |a| 를 0.5~3 Hz 대역 통과한 값
 * - 걸음 시점: 대역 통과 값의 국소 최대 중 [STEP_PEAK_MIN] 이상, 간격 [MIN_STEP_INTERVAL_MS] 이상. 창 안에서 상태 없이 세므로
 *   겹치는 창(4초 창, 2초 주기)에서는 같은 걸음이 두 창에 실린다(EC2 가 시각으로 합친다)
 * - 시작·멈춤 전환: 창의 새 절반(마지막 2초)만 0.5초 블록 RMS 로 판정한다(히스테리시스가 창을 넘어 이어져 중복이 없다).
 *   시작은 한 블록이면 되고 멈춤은 조용한 블록 [STILL_BLOCKS_TO_STOP]개가 이어져야 한다(실기기에서 걷는 중 한 블록이 조용해 STOP·START 가 깜빡였다)
 * - 최대 주파수: 10 Hz 대역 시계열에 0.05 Hz 간격 DFT, 평균의 [PEAK_PROMINENCE] 배 이상인 최대 성분
 * - 샘플 시각은 오름차순이어야 한다(역행·중복은 버린다). 공백 뒤에는 필터를 비워 공백 양끝의 차이가 걸음으로 보이지 않게 한다
 */
class GaitAnalyzer(private val sampleRateHz: Int = SAMPLE_RATE_HZ) {
    private val filter = BandPassFilter(sampleRateHz.toDouble())
    private val times = ArrayDeque<Long>()
    private val values = ArrayDeque<Double>()

    /** 걷는 중으로 보는 상태. 창을 넘어 이어진다 */
    var moving = false
        private set

    /** 걷는 중에 이어진 조용한 블록 수 */
    private var stillBlocks = 0

    /** 마지막으로 넣은 샘플 시각. 없으면 null */
    val latestSampleMs: Long? get() = times.lastOrNull()

    private val gapMs = GAP_SAMPLE_PERIODS * 1000L / sampleRateHz

    /** 샘플 하나(워치 시계 ms, m/s²). 유한하지 않은 값, 시각이 역행·중복인 샘플은 버린다 */
    fun add(tMs: Long, ax: Double, ay: Double, az: Double) {
        if (!ax.isFinite() || !ay.isFinite() || !az.isFinite()) return
        val last = times.lastOrNull()
        if (last != null && tMs <= last) return
        if (last != null && tMs - last > gapMs) filter.reset()
        times.addLast(tMs)
        values.addLast(filter.next(sqrt(ax * ax + ay * ay + az * az)))
        val keepFrom = tMs - RETAIN_MS
        while (times.isNotEmpty() && times.first() < keepFrom) {
            times.removeFirst()
            values.removeFirst()
        }
    }

    /** 샘플·필터·걷기 상태를 모두 비운다(센서를 껐다 다시 켤 때) */
    fun reset() {
        times.clear()
        values.clear()
        filter.reset()
        moving = false
        stillBlocks = 0
    }

    /** [windowEndMs] 로 끝나는 4초 창의 특징. 샘플이 [MIN_WINDOW_FILL] 만큼 차지 않았으면 null */
    fun window(windowEndMs: Long): GaitFeatures? {
        val start = windowEndMs - WINDOW_MS
        // 시각이 오름차순이라 뒤에서부터 창 범위만 꺼낸다(30초 보관분을 다 훑지 않는다)
        val t = ArrayList<Long>()
        val v = ArrayList<Double>()
        var i = times.size - 1
        while (i >= 0 && times[i] >= windowEndMs) i--
        while (i >= 0 && times[i] >= start) {
            t += times[i]
            v += values[i]
            i--
        }
        t.reverse()
        v.reverse()
        if (t.size < WINDOW_MS / 1000.0 * sampleRateHz * MIN_WINDOW_FILL) return null
        val (band, sampled) = resample(t, v, start)
        val sampledCount = sampled.count { it }
        // RMS 는 샘플이 있는 칸만으로(빈 칸의 0 은 모름이지 정지가 아니다)
        val rms = sqrt(band.indices.filter { sampled[it] }.sumOf { band[it] * band[it] } / sampledCount)
        return GaitFeatures(
            windowStartMs = start,
            windowEndMs = windowEndMs,
            sampleRateHz = sampleRateHz,
            stepTimesMs = detectSteps(t, v),
            transitions = updateMotion(t, v, windowEndMs - HOP_MS, windowEndMs),
            bandRateHz = BAND_RATE_HZ,
            bandValues = band,
            rms = rms,
            peakFreqHz = if (rms >= MOVING_RMS_OFF) peakFrequency(band, sampled) else null,
            moving = moving,
        )
    }

    private fun detectSteps(t: List<Long>, v: List<Double>): List<Long> {
        val steps = ArrayList<Long>()
        var last: Long? = null
        for (i in 1 until v.size - 1) {
            val peak = v[i] >= STEP_PEAK_MIN && v[i] > v[i - 1] && v[i] >= v[i + 1]
            if (peak && (last == null || t[i] - last >= MIN_STEP_INTERVAL_MS)) {
                steps += t[i]
                last = t[i]
                if (steps.size == MAX_STEPS_PER_WINDOW) break
            }
        }
        return steps
    }

    /** 창 시작부터 1/[BAND_RATE_HZ] 간격의 칸마다 평균과, 칸에 샘플이 있었는지. 샘플이 없는 칸은 0(계약상 수여야 한다) */
    private fun resample(t: List<Long>, v: List<Double>, start: Long): Pair<List<Double>, BooleanArray> {
        val binMs = 1000L / BAND_RATE_HZ
        val bins = (WINDOW_MS / binMs).toInt()
        val sum = DoubleArray(bins)
        val count = IntArray(bins)
        for (i in t.indices) {
            val bin = ((t[i] - start) / binMs).toInt()
            if (bin in 0 until bins) {
                sum[bin] += v[i]
                count[bin]++
            }
        }
        return List(bins) { if (count[it] == 0) 0.0 else sum[it] / count[it] } to BooleanArray(bins) { count[it] > 0 }
    }

    /** 10 Hz 대역 시계열(샘플 있는 칸만)에 DFT. 대역이 3 Hz 까지라 10 Hz 시계열이면 충분하고 원본 50 Hz 보다 5배 싸다 */
    private fun peakFrequency(band: List<Double>, sampled: BooleanArray): Double? {
        var best = 0.0
        var bestFreq = 0.0
        var total = 0.0
        var n = 0
        var f = BAND_LOW_HZ
        while (f <= BAND_HIGH_HZ + 1e-9) {
            var re = 0.0
            var im = 0.0
            for (k in band.indices) {
                if (!sampled[k]) continue
                val phase = 2 * PI * f * k / BAND_RATE_HZ
                re += band[k] * cos(phase)
                im += band[k] * sin(phase)
            }
            val power = re * re + im * im
            total += power
            n++
            if (power > best) {
                best = power
                bestFreq = f
            }
            f += PEAK_FREQ_STEP_HZ
        }
        val mean = total / n
        return if (mean > 0 && best >= PEAK_PROMINENCE * mean) Math.round(bestFreq * 100) / 100.0 else null
    }

    private fun updateMotion(t: List<Long>, v: List<Double>, fromMs: Long, toMs: Long): List<MotionTransition> {
        val transitions = ArrayList<MotionTransition>()
        val blocks = ((toMs - fromMs) / MOTION_BLOCK_MS).toInt()
        val sum = DoubleArray(blocks)
        val count = IntArray(blocks)
        for (i in t.indices) {
            if (t[i] < fromMs) continue
            val block = ((t[i] - fromMs) / MOTION_BLOCK_MS).toInt()
            if (block in 0 until blocks) {
                sum[block] += v[i] * v[i]
                count[block]++
            }
        }
        for (b in 0 until blocks) {
            if (transitions.size >= MAX_TRANSITIONS_PER_WINDOW) break
            val blockStart = fromMs + b * MOTION_BLOCK_MS
            if (count[b] > 0) {
                val rms = sqrt(sum[b] / count[b])
                if (!moving && rms >= MOVING_RMS_ON) {
                    moving = true
                    stillBlocks = 0
                    transitions += MotionTransition(blockStart, MotionKind.START)
                } else if (moving && rms < MOVING_RMS_OFF) {
                    stillBlocks++
                    if (stillBlocks >= STILL_BLOCKS_TO_STOP) {
                        moving = false
                        stillBlocks = 0
                        // 멈춘 시점은 조용해지기 시작한 블록
                        transitions += MotionTransition(blockStart - (STILL_BLOCKS_TO_STOP - 1) * MOTION_BLOCK_MS, MotionKind.STOP)
                    }
                } else if (moving) {
                    stillBlocks = 0
                }
            }
        }
        return transitions
    }
}
