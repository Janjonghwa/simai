package com.ssafy.simai.watch.alert

private const val LONG_ON_MS = 800L
private const val LONG_OFF_MS = 200L
private const val SHORT_ON_MS = 150L
private const val SHORT_GAP_MS = 120L
private const val WEAK_ON_MS = 120L
private const val FULL = 255
private const val WEAK = 80
private const val OFF = 0

/**
 * 진동 패턴(FR-45, 부록 C.6). 길이는 꺼짐·켜짐이 번갈아 나오고 첫 칸은 시작 지연이다.
 *
 * - DANGER_LONG: 위험. 길게 연속. 확인하거나 기한이 지날 때까지 되풀이한다
 * - CAUTION_DOUBLE: 경고. 짧게 두 번
 * - CAUTION_WEAK: 주의. 약하게 한 번. 명세의 vibration_pattern 값(DB 표)에는 없고 앱 안에서만 쓴다
 */
enum class VibrationPattern(
    val timingsMs: List<Long>,
    /** timingsMs 와 같은 길이. 0 은 꺼짐, 255 는 가장 세게 */
    val amplitudes: List<Int>,
    /** 이 위치부터 되풀이한다. null 이면 한 번만 */
    val repeatFrom: Int?,
) {
    DANGER_LONG(listOf(0L, LONG_ON_MS, LONG_OFF_MS), listOf(OFF, FULL, OFF), repeatFrom = 1),
    CAUTION_DOUBLE(
        listOf(0L, SHORT_ON_MS, SHORT_GAP_MS, SHORT_ON_MS),
        listOf(OFF, FULL, OFF, FULL),
        repeatFrom = null,
    ),
    CAUTION_WEAK(listOf(0L, WEAK_ON_MS), listOf(OFF, WEAK), repeatFrom = null),
    ;

    companion object {
        /** 패턴을 따로 주지 않았을 때 등급으로 정한다 */
        fun forSeverity(severity: AlertSeverity): VibrationPattern = when (severity) {
            AlertSeverity.CRITICAL -> DANGER_LONG
            AlertSeverity.HIGH -> CAUTION_DOUBLE
            AlertSeverity.CAUTION -> CAUTION_WEAK
        }
    }
}
