package com.ssafy.simai.watch.ui

import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.material3.Text

private val WAVE_COLOR = Color(0xFF7CD992)

/** 손을 좌우로 흔드는 각도(도)와 한 번 흔드는 시간(ms) */
private const val WAVE_DEGREES = 22f
private const val WAVE_HALF_PERIOD_MS = 450

/** 화면 너비(u) 대비 크기 */
private const val HAND_SIZE = 0.34f
private const val TITLE_SP = 0.085f
private const val GAP = 0.04f

/**
 * 배정 안내(S15P31A101-510): "워치 찬 손을 3초 흔들어 주세요" 와 좌우로 흔드는 손 그림. 연결됐는데 배정이 없을 때 기본 화면 첫 칸을 대신한다.
 * 손은 손목(아래 가운데)을 축으로 좌우로 흔들리는 단순한 도형이라 색을 못 알아봐도 읽힌다.
 */
@Composable
fun WaveGuide(u: Dp, modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "wave")
    val angle by transition.animateFloat(
        initialValue = -WAVE_DEGREES,
        targetValue = WAVE_DEGREES,
        animationSpec = infiniteRepeatable(tween(WAVE_HALF_PERIOD_MS, easing = FastOutSlowInEasing), RepeatMode.Reverse),
        label = "angle",
    )
    Column(
        modifier = modifier.fillMaxWidth(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(u * GAP, Alignment.CenterVertically),
    ) {
        Canvas(Modifier.size(u * HAND_SIZE)) {
            val w = size.width
            val h = size.height
            val wrist = Offset(w / 2, h * 0.94f)
            rotate(degrees = angle, pivot = wrist) {
                // 팔뚝
                drawLine(WAVE_COLOR, wrist, Offset(w / 2, h * 0.58f), strokeWidth = w * 0.16f, cap = StrokeCap.Round)
                // 손바닥
                drawRoundRect(
                    WAVE_COLOR,
                    topLeft = Offset(w * 0.28f, h * 0.3f),
                    size = Size(w * 0.44f, h * 0.34f),
                    cornerRadius = CornerRadius(w * 0.1f, w * 0.1f),
                )
                // 손가락 네 개(가운데 둘이 더 길다)
                for (i in 0 until 4) {
                    val x = w * (0.335f + 0.11f * i)
                    val tip = if (i == 1 || i == 2) h * 0.06f else h * 0.12f
                    drawLine(WAVE_COLOR, Offset(x, h * 0.36f), Offset(x, tip), strokeWidth = w * 0.085f, cap = StrokeCap.Round)
                }
                // 엄지
                drawLine(WAVE_COLOR, Offset(w * 0.3f, h * 0.52f), Offset(w * 0.12f, h * 0.38f), strokeWidth = w * 0.085f, cap = StrokeCap.Round)
            }
        }
        Text(
            "워치 찬 손을\n3초 흔들어 주세요",
            color = Color.White,
            fontSize = (u * TITLE_SP).value.sp,
            fontWeight = FontWeight.Bold,
            textAlign = TextAlign.Center,
        )
    }
}

private const val CHECK_SIZE = 0.3f

/** 배정되고 잠깐 보이는 "연결됨"(S15P31A101-510). 확인 표시와 한 단어 */
@Composable
fun BoundConfirm(u: Dp, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier.fillMaxWidth(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(u * GAP, Alignment.CenterVertically),
    ) {
        AlertIcon(AlertIconShape.CHECK, WAVE_COLOR, Modifier.size(u * CHECK_SIZE))
        Text("연결됨", color = WAVE_COLOR, fontSize = (u * TITLE_SP * 1.3f).value.sp, fontWeight = FontWeight.Bold, textAlign = TextAlign.Center)
    }
}
