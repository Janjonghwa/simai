package com.ssafy.simai.watch.ui

import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import kotlin.math.cos
import kotlin.math.sin

/** 아이콘 바탕 원의 투명도(등급색을 검정에 옅게 깐다) */
private const val CONTAINER_ALPHA = 0.16f

/** 아이콘 모양의 반지름·선 굵기(바탕 원 지름 대비) */
private const val SYMBOL_RADIUS = 0.27f
private const val STROKE = 0.065f

private const val OCTAGON_SIDES = 8
private const val OCTAGON_START_DEG = 22.5f
private const val TRIANGLE_SIDES = 3
private const val TRIANGLE_START_DEG = -90f
private const val FULL_TURN_DEG = 360f

/** 느낌표의 세로 위치(모양 반지름 대비). 삼각형은 무게중심이 아래라 조금 내린다 */
private const val BANG_TOP = -0.42f
private const val BANG_BOTTOM = 0.12f
private const val BANG_DOT = 0.46f
private const val TRIANGLE_BANG_SHIFT = 0.16f

/**
 * 등급 아이콘: 옅은 바탕 원 안에 등급 모양을 그린다. 색을 알아보지 못해도 모양으로 등급을 안다.
 * 위험은 채운 팔각형, 경고는 삼각형 윤곽, 주의는 원 윤곽이고 모두 느낌표가 들어 있다. [AlertIconShape.CHECK] 는 확인함 표시다.
 */
@Composable
fun AlertIcon(shape: AlertIconShape, color: Color, modifier: Modifier = Modifier) {
    Canvas(modifier) {
        val diameter = size.minDimension
        val center = Offset(size.width / 2, size.height / 2)
        val radius = diameter * SYMBOL_RADIUS
        val stroke = diameter * STROKE
        drawCircle(color.copy(alpha = CONTAINER_ALPHA), radius = diameter / 2, center = center)
        when (shape) {
            AlertIconShape.OCTAGON_FILLED -> {
                drawPath(polygon(center, radius, OCTAGON_SIDES, OCTAGON_START_DEG), color)
                drawBang(center, radius, Color.Black, stroke, shift = 0f)
            }
            AlertIconShape.TRIANGLE_OUTLINE -> {
                drawPath(
                    polygon(center, radius * TRIANGLE_SCALE, TRIANGLE_SIDES, TRIANGLE_START_DEG),
                    color,
                    style = Stroke(stroke, cap = StrokeCap.Round, join = StrokeJoin.Round),
                )
                drawBang(center, radius, color, stroke, shift = TRIANGLE_BANG_SHIFT)
            }
            AlertIconShape.CIRCLE_OUTLINE -> {
                drawCircle(color, radius, center, style = Stroke(stroke))
                drawBang(center, radius, color, stroke, shift = 0f)
            }
            AlertIconShape.CHECK -> drawCheck(center, radius, color, stroke)
        }
    }
}

/** 삼각형은 같은 반지름이면 팔각형보다 작아 보여서 키운다 */
private const val TRIANGLE_SCALE = 1.2f

private fun polygon(center: Offset, radius: Float, sides: Int, startDeg: Float): Path = Path().apply {
    for (i in 0 until sides) {
        val angle = Math.toRadians((startDeg + i * FULL_TURN_DEG / sides).toDouble())
        val x = center.x + radius * cos(angle).toFloat()
        val y = center.y + radius * sin(angle).toFloat()
        if (i == 0) moveTo(x, y) else lineTo(x, y)
    }
    close()
}

private fun DrawScope.drawBang(center: Offset, radius: Float, color: Color, stroke: Float, shift: Float) {
    val dy = radius * shift
    drawLine(
        color,
        Offset(center.x, center.y + radius * BANG_TOP + dy),
        Offset(center.x, center.y + radius * BANG_BOTTOM + dy),
        strokeWidth = stroke,
        cap = StrokeCap.Round,
    )
    drawCircle(color, radius = stroke * 0.6f, center = Offset(center.x, center.y + radius * BANG_DOT + dy))
}

private fun DrawScope.drawCheck(center: Offset, radius: Float, color: Color, stroke: Float) {
    val path = Path().apply {
        moveTo(center.x - radius * 0.55f, center.y + radius * 0.05f)
        lineTo(center.x - radius * 0.12f, center.y + radius * 0.48f)
        lineTo(center.x + radius * 0.6f, center.y - radius * 0.42f)
    }
    drawPath(path, color, style = Stroke(stroke * 1.3f, cap = StrokeCap.Round, join = StrokeJoin.Round))
}
