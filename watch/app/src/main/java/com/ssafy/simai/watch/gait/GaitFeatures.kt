package com.ssafy.simai.watch.gait

/** 걸음 특징 창 길이와 주기(docs/design/watch-i4-contract.md: 4초 창을 2초마다, 절반 겹침) */
const val WINDOW_MS = 4_000L
const val HOP_MS = 2_000L

/** 가속도 원본 샘플 주기(Hz)와 대역 시계열 주기(Hz, 계약 band_magnitude.rate_hz 기본 10) */
const val SAMPLE_RATE_HZ = 50
const val BAND_RATE_HZ = 10

/** 계약 상한: 창 안 걸음 시점 64, 전환 16 */
const val MAX_STEPS_PER_WINDOW = 64
const val MAX_TRANSITIONS_PER_WINDOW = 16

enum class MotionKind { START, STOP }

/** 걷기 시작·멈춤이 바뀐 시점(워치 시계) */
data class MotionTransition(val atMs: Long, val kind: MotionKind)

/**
 * 한 창의 걸음 특징(계약 gait_features 의 payload 값, 시계 차이만 뺀 것). 모든 시각은 워치 시계 UTC ms.
 *
 * @property bandValues 0.5~3 Hz 대역 통과한 |a| 를 [bandRateHz] 로 다시 표본화한 값(창 시작부터)
 * @property peakFreqHz 대역 안 최대 성분(걸음 주기). 뚜렷한 성분이 없으면(서 있음) null
 * @property moving 창이 끝난 시점에 걷는 중으로 보는 상태(시작/멈춤 전환과 같은 히스테리시스 상태). 상태 화면용
 */
data class GaitFeatures(
    val windowStartMs: Long,
    val windowEndMs: Long,
    val sampleRateHz: Int,
    val stepTimesMs: List<Long>,
    val transitions: List<MotionTransition>,
    val bandRateHz: Int,
    val bandValues: List<Double>,
    val rms: Double,
    val peakFreqHz: Double?,
    val moving: Boolean,
)
