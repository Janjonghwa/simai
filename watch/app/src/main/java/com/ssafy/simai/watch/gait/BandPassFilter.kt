package com.ssafy.simai.watch.gait

import kotlin.math.PI

/**
 * 0.5~3 Hz 대역 통과 필터(계약 gait_features.band_magnitude). 1차 고역(중력·자세 성분 제거)과 1차 저역(손떨림·충격 제거)을
 * 각각 두 번 거는 단순한 IIR 이라 결정적이고 JVM 에서 시험한다. 걸음(1~2.5 Hz) 성분은 약 0.6~0.7 배로 남고 그 밖은 깎인다.
 */
class BandPassFilter(sampleRateHz: Double, lowHz: Double = BAND_LOW_HZ, highHz: Double = BAND_HIGH_HZ) {
    private val dt = 1.0 / sampleRateHz
    private val highPassAlpha = (1.0 / (2 * PI * lowHz)).let { rc -> rc / (rc + dt) }
    private val lowPassAlpha = (1.0 / (2 * PI * highHz)).let { rc -> dt / (rc + dt) }

    private val highPass = arrayOf(HighPass(), HighPass())
    private val lowPass = arrayOf(LowPass(), LowPass())

    /** 샘플 공백(CPU 수면) 뒤에는 상태를 비운다. 이어 붙이면 공백 양끝의 차이가 걸음처럼 보인다 */
    fun reset() {
        for (stage in highPass) stage.reset()
        for (stage in lowPass) stage.reset()
    }

    /** 샘플 하나를 넣고 대역 통과 값을 받는다 */
    fun next(x: Double): Double {
        var y = x
        for (stage in highPass) y = stage.next(y)
        for (stage in lowPass) y = stage.next(y)
        return y
    }

    private inner class HighPass {
        private var started = false
        private var prevX = 0.0
        private var prevY = 0.0

        fun reset() {
            started = false
        }

        fun next(x: Double): Double {
            // 첫 샘플은 그대로 기준으로 삼아 시작 순간의 큰 튐을 막는다
            if (!started) {
                started = true
                prevX = x
                prevY = 0.0
                return 0.0
            }
            val y = highPassAlpha * (prevY + x - prevX)
            prevX = x
            prevY = y
            return y
        }
    }

    private inner class LowPass {
        private var y = 0.0

        fun reset() {
            y = 0.0
        }

        fun next(x: Double): Double {
            y += lowPassAlpha * (x - y)
            return y
        }
    }
}

/** 대역의 아래·위(계약 band_magnitude: 0.5~3 Hz) */
const val BAND_LOW_HZ = 0.5
const val BAND_HIGH_HZ = 3.0
