package com.ssafy.simai.watch.ui

import android.text.format.DateFormat
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.material3.AppScaffold
import androidx.wear.compose.material3.MaterialTheme
import androidx.wear.compose.material3.ScreenScaffold
import androidx.wear.compose.material3.Text
import com.ssafy.simai.watch.BuildConfig
import com.ssafy.simai.watch.binding.BindingView
import com.ssafy.simai.watch.config.WatchConfig
import com.ssafy.simai.watch.config.WorkerLabel
import com.ssafy.simai.watch.i4.ConnectionStatus
import com.ssafy.simai.watch.i4.GaitSummary
import com.ssafy.simai.watch.i4.LinkState
import kotlinx.coroutines.delay
import java.util.Locale

private val NOT_CONFIGURED_COLOR = Color(0xFFFFD600)
private val CONNECTED_COLOR = Color(0xFF7CD992)
private val DISCONNECTED_COLOR = Color(0xFFFF6B6B)
private val SECONDARY_TEXT = Color(0xFFB3B3B3)
private val DEV_TEXT = Color(0xFF8A8A8A)

// 어둡게 켜져 있는 화면의 회색. 흑백 두 색만 내는 화면(lowBit)에서는 흰색을 쓴다
private val AMBIENT_CLOCK_TEXT = Color(0xFFCFCFCF)
private val AMBIENT_NAME_TEXT = Color(0xFFBDBDBD)
private val AMBIENT_ZONE_TEXT = Color(0xFF8A8A8A)

// 첫 화면의 크기는 알림 화면처럼 화면 너비(u) 대비 비율이다
private const val CLOCK_SP = 0.25f
private const val NAME_SP = 0.08f
private const val ZONE_SP = 0.07f
private const val FACE_GAP = 0.03f

private const val DEV_PAD = 0.12f
private const val STATE_SP = 22
private const val LAST_ALERT_SP = 15
private const val DEV_SP = 12

/**
 * 기본(대기) 화면. 첫 화면은 큰 시계와 착용자 이름·구역이다. 연결 상태([status], S15P31A101-337)와 마지막 알림, 개발용 값(EC2·기기 ID·하트비트·배터리·버전)은
 * 아래로 스크롤해야 나온다.
 * 이름·구역은 값이 있을 때만 보인다(값을 줄 계약 -334 가 없어 지금은 debug 입구로만 넣는다).
 *
 * [ambient] 가 있으면 손목을 내려 어둡게 켜져 있는 화면이다: 얇은 회색 큰 시계와 이름·구역만 남기고, 번인 방지로 위치를 조금씩 옮긴다.
 * [tickMs] 는 앰비언트에서 시스템이 분마다 알려 주는 시각이다.
 */
@Composable
fun StatusScreen(
    config: WatchConfig?,
    lastAlertAtMs: Long?,
    label: WorkerLabel?,
    ambient: AmbientState?,
    tickMs: Long,
    status: ConnectionStatus = ConnectionStatus(),
    binding: BindingView = BindingView(),
    alertPending: Boolean = false,
) {
    MaterialTheme {
        if (ambient == null) {
            InteractiveScreen(config, lastAlertAtMs, label, tickMs, status, binding, alertPending)
        } else {
            AmbientScreen(label, ambient, tickMs)
        }
    }
}

@Composable
private fun InteractiveScreen(
    config: WatchConfig?,
    lastAlertAtMs: Long?,
    label: WorkerLabel?,
    tickMs: Long,
    status: ConnectionStatus,
    binding: BindingView,
    alertPending: Boolean,
) {
    // 큰 시계가 있어서 위쪽 기본 시계는 끈다
    AppScaffold(timeText = {}) {
        val scroll = rememberScrollState()
        ScreenScaffold(scrollState = scroll) {
            BoxWithConstraints(modifier = Modifier.fillMaxSize()) {
                val u = maxWidth
                val faceHeight = maxHeight
                Column(
                    modifier = Modifier.fillMaxSize().verticalScroll(scroll),
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    // 첫 칸: 연결됐는데 배정이 없으면 손 흔들기 안내, 배정된 직후엔 "연결됨", 아니면 시계(S15P31A101-510). 확인 대기 알림이 있으면 안내하지 않는다
                    when {
                        binding.showGuide(status.state == LinkState.CONNECTED, alertPending) -> WaveGuide(u, Modifier.height(faceHeight))
                        binding.showConnected(alertPending) -> BoundConfirm(u, Modifier.height(faceHeight))
                        else -> Face(label, u, Modifier.height(faceHeight), ambient = null, tickMs = tickMs)
                    }
                    DevLines(config, lastAlertAtMs, status, u)
                }
            }
        }
    }
}

@Composable
private fun AmbientScreen(label: WorkerLabel?, ambient: AmbientState, tickMs: Long) {
    BoxWithConstraints(modifier = Modifier.fillMaxSize().background(Color.Black)) {
        Face(label, maxWidth, Modifier.fillMaxSize(), ambient, tickMs)
    }
}

@Composable
private fun Face(label: WorkerLabel?, u: Dp, modifier: Modifier, ambient: AmbientState?, tickMs: Long) {
    val nowMs = rememberMinuteClockMs(tickMs)
    val (shiftX, shiftY) = if (ambient?.burnInProtection == true) burnInShiftDp(nowMs) else 0 to 0
    Column(
        modifier = modifier.fillMaxWidth().offset(shiftX.dp, shiftY.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(u * FACE_GAP, Alignment.CenterVertically),
    ) {
        BigClock(nowMs, u, ambient)
        if (label != null) NameAndZone(label, u, ambient)
    }
}

@Composable
private fun BigClock(nowMs: Long, u: Dp, ambient: AmbientState?) {
    // 어둡게 켜져 있을 때는 얇고 어두운 회색으로 켜진 픽셀을 줄인다(글자 윤곽선은 겹친 획이 비쳐 쓰지 않는다)
    Text(
        bigClockText(nowMs, DateFormat.is24HourFormat(LocalContext.current)),
        color = if (ambient == null) Color.White else ambientColor(ambient, AMBIENT_CLOCK_TEXT),
        fontSize = (u * CLOCK_SP).value.sp,
        fontWeight = if (ambient == null) FontWeight.Bold else FontWeight.Normal,
        maxLines = 1,
    )
}

@Composable
private fun NameAndZone(label: WorkerLabel, u: Dp, ambient: AmbientState?) {
    if (ambient == null) {
        Text(label.name, color = Color.White, fontSize = (u * NAME_SP).value.sp, fontWeight = FontWeight.Bold, maxLines = 1)
        Text(label.zone, color = SECONDARY_TEXT, fontSize = (u * ZONE_SP).value.sp, maxLines = 1)
    } else {
        Text(label.name, color = ambientColor(ambient, AMBIENT_NAME_TEXT), fontSize = (u * NAME_SP).value.sp, maxLines = 1)
        Text(label.zone, color = ambientColor(ambient, AMBIENT_ZONE_TEXT), fontSize = (u * ZONE_SP).value.sp, maxLines = 1)
    }
}

private fun ambientColor(ambient: AmbientState, gray: Color): Color = if (ambient.lowBit) Color.White else gray

/** 지금 시각(ms). 분이 바뀔 때, 그리고 [tickMs] 가 바뀔 때(앰비언트의 분 갱신) 읽는다. */
@Composable
private fun rememberMinuteClockMs(tickMs: Long): Long {
    var nowMs by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(tickMs) {
        while (true) {
            nowMs = System.currentTimeMillis()
            delay(msUntilNextMinute(nowMs))
        }
    }
    return nowMs
}

@Composable
private fun DevLines(config: WatchConfig?, lastAlertAtMs: Long?, status: ConnectionStatus, u: Dp) {
    val (stateText, stateColor) = when {
        config == null -> "설정 안 됨" to NOT_CONFIGURED_COLOR
        status.state == LinkState.CONNECTED -> "연결됨" to CONNECTED_COLOR
        status.state == LinkState.CONNECTING -> "연결 중" to Color.White
        else -> "연결 끊김" to DISCONNECTED_COLOR
    }
    Column(
        modifier = Modifier.fillMaxWidth().padding(horizontal = u * DEV_PAD, vertical = u * DEV_PAD),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Line(stateText, stateColor, STATE_SP, FontWeight.Bold)
        Line(if (lastAlertAtMs == null) "알림 없음" else "마지막 알림 ${clockText(lastAlertAtMs)}", SECONDARY_TEXT, LAST_ALERT_SP)
        // 토큰 값은 화면에 보이지 않는다
        Line(if (config == null) "adb 로 주소·ID·토큰을 넣는다" else "EC2 ${config.ec2Host} · ${config.deviceId}", DEV_TEXT, DEV_SP)
        if (config != null) {
            Line(heartbeatLine(status), DEV_TEXT, DEV_SP)
            Line("배터리 ${status.batteryPct?.let { "$it%" } ?: "-"} · ${status.transport.name}", DEV_TEXT, DEV_SP)
            status.lastFailure?.let { Line(it, DEV_TEXT, DEV_SP) }
            status.lastGait?.let { Line(gaitLine(it), DEV_TEXT, DEV_SP) }
        }
        Line("v${BuildConfig.VERSION_NAME}", DEV_TEXT, DEV_SP)
    }
}

/** 마지막 걸음 특징 창: "걸음 7/4초 · 1.8Hz · 걷는 중" */
private fun gaitLine(gait: GaitSummary): String {
    // 로케일에 따라 소수점이 쉼표가 되지 않게
    val peak = gait.peakFreqHz?.let { " · ${String.format(Locale.ROOT, "%.1f", it)}Hz" } ?: ""
    return "걸음 ${gait.steps}/4초$peak · ${if (gait.moving) "걷는 중" else "멈춤"} · ${clockText(gait.windowEndMs)}"
}

private fun heartbeatLine(status: ConnectionStatus): String {
    val last = status.lastEc2AtMs?.let { "EC2 하트비트 ${clockText(it)}" } ?: "EC2 하트비트 없음"
    val offset = status.clockOffsetMs?.let { " · 시계차 ${if (it >= 0) "+" else ""}${it}ms" } ?: ""
    return last + offset
}

@Composable
private fun Line(text: String, color: Color, sizeSp: Int, weight: FontWeight = FontWeight.Normal) {
    Text(text, color = color, fontSize = sizeSp.sp, fontWeight = weight, textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth())
}
