package com.ssafy.simai.watch.i4

/** 재연결 대기 시간: 1·2·4·8·16·30초로 늘고 연결에 성공하면 처음으로 돌아간다 */
class Backoff(private val initialMs: Long = 1_000L, private val maxMs: Long = 30_000L) {
    private var currentMs = initialMs

    /** 이번에 기다릴 시간. 부를 때마다 두 배로 늘어 상한에서 멈춘다 */
    fun next(): Long {
        val delay = currentMs
        currentMs = (currentMs * 2).coerceAtMost(maxMs)
        return delay
    }

    fun reset() {
        currentMs = initialMs
    }
}
