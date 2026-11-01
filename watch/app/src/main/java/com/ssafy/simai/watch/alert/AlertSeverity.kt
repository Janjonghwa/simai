package com.ssafy.simai.watch.alert

/**
 * 워치로 오는 알림 등급. 관제 앱과 같은 이름이다(CRITICAL 위험, HIGH 경고, CAUTION 주의).
 * rank 가 클수록 급하다. 알림이 여러 개면 rank 가 큰 것을 먼저 보인다.
 */
enum class AlertSeverity(val rank: Int) {
    CAUTION(1),
    HIGH(2),
    CRITICAL(3),
}
