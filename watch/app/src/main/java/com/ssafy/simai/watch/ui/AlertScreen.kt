package com.ssafy.simai.watch.ui

import android.provider.Settings
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.material3.MaterialTheme
import androidx.wear.compose.material3.Text
import com.ssafy.simai.watch.alert.AlertPhase
import com.ssafy.simai.watch.alert.AlertSeverity
import com.ssafy.simai.watch.alert.TrackedAlert

// 한눈에 읽는 화면: 검정 바탕에 등급색은 링·아이콘·등급 글자에만 쓴다. 크기는 모두 화면 너비(u) 대비 비율이라
// 192dp 에서 227dp 까지 둥근 화면에서 같은 모양으로 보인다.

private val ACK_FILL = Color(0xFF2E2E2E)
private val ACKED_COLOR = Color(0xFF7CD992)
private val SECONDARY_TEXT = Color(0xFFB3B3B3)

/** 남은 시간 링: 12시 방향 왼쪽 아래(7시)에서 시계 방향으로 300도, 아래 60도는 비워 확인 단추 자리를 둔다 */
private const val RING_START_DEG = 120f
private const val RING_SWEEP_DEG = 300f
private const val RING_STROKE = 0.03f
private const val RING_INSET = 0.012f
private const val TRACK_ALPHA = 0.22f

private const val TOP_PAD = 0.08f
private const val ICON_SIZE = 0.34f
private const val ACKED_ICON_SIZE = 0.3f
private const val GAP = 0.012f
private const val LABEL_SP = 0.065f
private const val HEADLINE_SP = 0.105f
private const val BADGE_SIZE = 0.125f
private const val BADGE_SP = 12

private const val ACK_WIDTH = 0.76f
private const val ACK_HEIGHT = 0.235f
private const val ACK_CORNER = 0.085f
private const val ACK_TEXT_SP = 0.075f
private const val ACK_TEXT_TOP = 0.2f

/** 위험 아이콘이 천천히 맥박친다: 1.0 에서 1.06 배까지 커졌다 작아지는 데 1.2초(초당 3회 미만) */
private const val PULSE_SCALE = 1.06f
private const val PULSE_HALF_MS = 600

/**
 * 위험 알림 화면. 위에서 아래로: 큰 등급 아이콘, 등급 글자(+확인 필요), 큰 한 단어(방향 화살표가 붙는다), 아래쪽 큰 확인 단추.
 * 가장자리 링이 확인까지 남은 시간을 보이고, 기한이 지나면 한 바퀴 차서 "기한 지남 +0:12" 가 된다.
 * [nowMs] 는 [TrackedAlert.receivedAtMs] 와 같은 단조 시계다.
 */
@Composable
fun AlertScreen(tracked: TrackedAlert, others: Int, nowMs: Long, onAck: () -> Unit) {
    val alert = tracked.alert
    val style = severityStyle(alert.severity)
    val expired = tracked.phase == AlertPhase.EXPIRED
    MaterialTheme {
        BoxWithConstraints(modifier = Modifier.fillMaxSize().clip(CircleShape).background(Color.Black)) {
            val u = maxWidth
            CountdownRing(style.color, if (expired) 1f else tracked.remainingFraction(nowMs))
            Column(
                modifier = Modifier.fillMaxSize().padding(top = u * TOP_PAD, bottom = u * ACK_HEIGHT),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(u * GAP, Alignment.CenterVertically),
            ) {
                IconWithBadge(style, others, u, pulse = alert.severity == AlertSeverity.CRITICAL)
                Text(
                    statusLine(tracked, style.label, expired, nowMs),
                    color = style.color,
                    fontSize = (u * LABEL_SP).value.sp,
                    fontWeight = FontWeight.Bold,
                    maxLines = 1,
                )
                val line = wordLine(alert.word, alert.direction)
                Text(
                    line,
                    color = Color.White,
                    fontSize = wordFontSizeSp(u.value, line.length).sp,
                    fontWeight = FontWeight.Bold,
                    textAlign = TextAlign.Center,
                    maxLines = 1,
                )
            }
            AckButton(u, onAck)
        }
    }
}

/** 확인한 직후에 잠깐 보이는 화면. */
@Composable
fun AckedScreen(ackedAtMs: Long) {
    MaterialTheme {
        BoxWithConstraints(modifier = Modifier.fillMaxSize().background(Color.Black), contentAlignment = Alignment.Center) {
            val u = maxWidth
            Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(u * GAP * 3)) {
                AlertIcon(AlertIconShape.CHECK, ACKED_COLOR, Modifier.size(u * ACKED_ICON_SIZE))
                Text("확인함", color = Color.White, fontSize = (u * HEADLINE_SP).value.sp, fontWeight = FontWeight.Bold)
                Text(clockText(ackedAtMs), color = SECONDARY_TEXT, fontSize = (u * LABEL_SP).value.sp)
            }
        }
    }
}

private fun statusLine(tracked: TrackedAlert, label: String, expired: Boolean, nowMs: Long): String = when {
    expired -> "기한 지남 ${overdueText(tracked.overdueMs(nowMs))}"
    tracked.alert.needsCheck -> "$label · 확인 필요"
    else -> label
}

@Composable
private fun IconWithBadge(style: SeverityStyle, others: Int, u: Dp, pulse: Boolean) {
    val iconSize = u * ICON_SIZE
    Box {
        AlertIcon(style.shape, style.color, Modifier.size(iconSize).scale(pulseScale(pulse)))
        if (others > 0) {
            Box(
                modifier = Modifier.align(Alignment.TopEnd).offset(x = iconSize * 0.1f, y = -(iconSize * 0.04f)).size(u * BADGE_SIZE)
                    .background(Color.White, CircleShape),
                contentAlignment = Alignment.Center,
            ) {
                Text("+$others", color = Color.Black, fontSize = BADGE_SP.sp, fontWeight = FontWeight.Bold, maxLines = 1)
            }
        }
    }
}

/** 맥박 배율. 사용자가 애니메이션을 껐으면(동작 줄이기) 늘 1 이다. */
@Composable
private fun pulseScale(active: Boolean): Float {
    val context = LocalContext.current
    val reduced = remember(context) {
        Settings.Global.getFloat(context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f
    }
    if (!active || reduced) return 1f
    val transition = rememberInfiniteTransition(label = "pulse")
    val scale by transition.animateFloat(
        initialValue = 1f,
        targetValue = PULSE_SCALE,
        animationSpec = infiniteRepeatable(tween(PULSE_HALF_MS, easing = FastOutSlowInEasing), RepeatMode.Reverse),
        label = "pulseScale",
    )
    return scale
}

@Composable
private fun CountdownRing(color: Color, fraction: Float) {
    Canvas(Modifier.fillMaxSize()) {
        val stroke = size.width * RING_STROKE
        val inset = stroke / 2 + size.width * RING_INSET
        val topLeft = Offset(inset, inset)
        val arcSize = Size(size.width - 2 * inset, size.height - 2 * inset)
        val style = Stroke(width = stroke, cap = StrokeCap.Round)
        drawArc(color.copy(alpha = TRACK_ALPHA), RING_START_DEG, RING_SWEEP_DEG, false, topLeft, arcSize, style = style)
        if (fraction > 0f) drawArc(color, RING_START_DEG, RING_SWEEP_DEG * fraction, false, topLeft, arcSize, style = style)
    }
}

/** 화면 아래쪽의 큰 확인 단추. 둥근 화면 가장자리에 잘려 반달 모양이 된다. 어느 등급 색 위에서도 보이게 회색 바탕에 흰 글자다. */
@Composable
private fun BoxScope.AckButton(u: Dp, onClick: () -> Unit) {
    val height = u * ACK_HEIGHT
    Box(
        modifier = Modifier.align(Alignment.BottomCenter).width(u * ACK_WIDTH).height(height)
            .clip(RoundedCornerShape(topStart = u * ACK_CORNER, topEnd = u * ACK_CORNER))
            .background(ACK_FILL)
            .clickable(onClickLabel = "확인", role = Role.Button, onClick = onClick),
        contentAlignment = Alignment.TopCenter,
    ) {
        Text(
            "확인",
            color = Color.White,
            fontSize = (u * ACK_TEXT_SP).value.sp,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.padding(top = height * ACK_TEXT_TOP),
        )
    }
}
