package com.ssafy.simai.watch.ui

import androidx.compose.ui.graphics.Color
import com.ssafy.simai.watch.alert.AlertSeverity

/** 등급색. 검정 바탕 위 대비가 위험 6.1:1, 경고 9.4:1, 주의 14.9:1 이다(Apple 다크 시스템색). */
private val RED = Color(0xFFFF4245)
private val ORANGE = Color(0xFFFF9230)
private val YELLOW = Color(0xFFFFD600)

/** 아이콘 모양. 색을 못 알아봐도(색약) 모양으로 등급을 안다: 채운 팔각형 > 삼각형 윤곽 > 원 윤곽 */
enum class AlertIconShape { OCTAGON_FILLED, TRIANGLE_OUTLINE, CIRCLE_OUTLINE, CHECK }

/** 등급 하나를 그리는 값: 글자, 색, 아이콘 모양. */
data class SeverityStyle(val label: String, val color: Color, val shape: AlertIconShape)

fun severityStyle(severity: AlertSeverity): SeverityStyle = when (severity) {
    AlertSeverity.CRITICAL -> SeverityStyle("위험", RED, AlertIconShape.OCTAGON_FILLED)
    AlertSeverity.HIGH -> SeverityStyle("경고", ORANGE, AlertIconShape.TRIANGLE_OUTLINE)
    AlertSeverity.CAUTION -> SeverityStyle("주의", YELLOW, AlertIconShape.CIRCLE_OUTLINE)
}
