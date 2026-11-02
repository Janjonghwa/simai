package com.ssafy.simai.watch

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.SystemClock
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import com.ssafy.simai.watch.service.AlertCenter
import com.ssafy.simai.watch.ui.AckedScreen
import com.ssafy.simai.watch.ui.AlertScreen
import kotlinx.coroutines.delay

/** 남은 시간 링을 다시 그리는 간격(ms) */
private const val TICK_MS = 200L

/** 확인한 뒤 "확인함 시각" 을 보이고 닫기까지(ms) */
private const val ACKED_SHOWN_MS = 2_000L

/**
 * 위험 알림 화면. 잠금 화면 위에 띄우고 화면을 켠다(매니페스트의 showWhenLocked·turnScreenOn).
 *
 * 알림 내용·진동·확인 전송은 [com.ssafy.simai.watch.service.I4Service] 가 맡고, 이 화면은 [AlertCenter] 의 보드를 관찰해
 * 보여 주고 확인 단추를 넘기기만 한다. 화면이 꺼져 있을 때는 서비스의 전체 화면 인텐트 알림이, 앱이 앞에 있을 때는 서비스가
 * 바로 띄운다. 확인 대기 알림이 모두 없어지면 잠깐 "확인함" 을 보이고 닫는다.
 */
class AlertActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContent { Content() }
    }

    @Composable
    private fun Content() {
        val board by AlertCenter.board.collectAsState()
        var nowMs by remember { mutableLongStateOf(SystemClock.elapsedRealtime()) }
        LaunchedEffect(Unit) {
            while (true) {
                delay(TICK_MS)
                nowMs = SystemClock.elapsedRealtime()
            }
        }
        val nothingPending = board.pending.isEmpty()
        LaunchedEffect(nothingPending) {
            if (nothingPending) {
                delay(ACKED_SHOWN_MS)
                finishAndRemoveTask()
            }
        }

        val current = board.current
        if (current != null) {
            AlertScreen(current, board.others, nowMs) { AlertCenter.acknowledge(current.alert.eventId) }
        } else {
            board.lastAcked?.ackedAtMs?.let { AckedScreen(it) }
        }
    }

    companion object {
        /** 알림 화면을 띄우는 인텐트. 서비스(알림·전체 화면 인텐트)가 쓴다. 알림 내용은 싣지 않는다(서비스가 들고 있다) */
        fun intent(context: Context): Intent =
            Intent(context, AlertActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }
}
