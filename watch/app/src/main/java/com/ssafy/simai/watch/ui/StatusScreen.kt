package com.ssafy.simai.watch.ui

import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.sp
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.foundation.lazy.rememberScalingLazyListState
import androidx.wear.compose.material3.AppScaffold
import androidx.wear.compose.material3.MaterialTheme
import androidx.wear.compose.material3.ScreenScaffold
import androidx.wear.compose.material3.Text
import com.ssafy.simai.watch.BuildConfig
import com.ssafy.simai.watch.config.WatchConfig

private val NOT_CONFIGURED_COLOR = Color(0xFFFFD600)
private val SECONDARY_TEXT = Color(0xFFB3B3B3)
private val DEV_TEXT = Color(0xFF8A8A8A)

private const val STATE_SP = 22
private const val LAST_ALERT_SP = 15
private const val DEV_SP = 12

/**
 * 대기 화면. 작업자는 연결 상태와 마지막 알림 시각만 보면 된다. 개발용 값(EC2 주소·토큰·버전)은 아래로 스크롤해야 나온다.
 * 연결은 S15P31A101-337 에서 붙이기 전이라 설정이 있어도 늘 "연결 안 됨" 이다.
 */
@Composable
fun StatusScreen(config: WatchConfig?, lastAlertAtMs: Long?) {
    MaterialTheme {
        AppScaffold {
            val listState = rememberScalingLazyListState()
            ScreenScaffold(scrollState = listState) { contentPadding ->
                ScalingLazyColumn(state = listState, contentPadding = contentPadding) {
                    item {
                        Line(
                            if (config == null) "설정 안 됨" else "연결 안 됨",
                            if (config == null) NOT_CONFIGURED_COLOR else Color.White,
                            STATE_SP,
                            FontWeight.Bold,
                        )
                    }
                    item { Line(if (lastAlertAtMs == null) "알림 없음" else "마지막 알림 ${clockText(lastAlertAtMs)}", SECONDARY_TEXT, LAST_ALERT_SP) }
                    // 토큰 값은 화면에 보이지 않는다
                    item { Line(if (config == null) "adb 로 주소·토큰을 넣는다" else "EC2 ${config.ec2Host} · 토큰 설정됨", DEV_TEXT, DEV_SP) }
                    item { Line("v${BuildConfig.VERSION_NAME}", DEV_TEXT, DEV_SP) }
                }
            }
        }
    }
}

@Composable
private fun Line(text: String, color: Color, sizeSp: Int, weight: FontWeight = FontWeight.Normal) {
    Text(text, color = color, fontSize = sizeSp.sp, fontWeight = weight, textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth())
}
