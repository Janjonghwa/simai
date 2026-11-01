package com.ssafy.simai.watch

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.ssafy.simai.watch.alert.AlertAcker
import com.ssafy.simai.watch.alert.AlertBoard
import com.ssafy.simai.watch.alert.AlertVibrator
import com.ssafy.simai.watch.alert.LastAlertStore
import com.ssafy.simai.watch.alert.LogAlertAcker
import com.ssafy.simai.watch.alert.WatchAlert
import com.ssafy.simai.watch.alert.alertFields
import com.ssafy.simai.watch.alert.putAlertFields
import com.ssafy.simai.watch.ui.AckedScreen
import com.ssafy.simai.watch.ui.AlertScreen
import kotlinx.coroutines.delay

/** 기한을 살피고 남은 시간 링을 다시 그리는 간격(ms) */
private const val TICK_MS = 200L

/** 확인한 뒤 "확인함 시각" 을 보이고 닫기까지(ms) */
private const val ACKED_SHOWN_MS = 2_000L

/**
 * 위험 알림 화면의 입구. 잠금 화면 위에 띄우고 화면을 켠다(매니페스트의 showWhenLocked·turnScreenOn).
 *
 * 알림은 [intentFor] 로 만든 인텐트로 받는다. 이미 떠 있으면 같은 화면이 새 알림을 더 받는다(singleTop).
 * 받은 알림은 [AlertBoard] 가 들고, 진동은 가장 급한 확인 대기 알림의 패턴으로 낸다.
 * EC2 연결(S15P31A101-337)이 붙으면 그 서비스가 이 인텐트로 알림을 넘기고 알림 보관도 서비스로 옮긴다.
 */
class AlertActivity : ComponentActivity() {
    private var board by mutableStateOf(AlertBoard())
    private var nowMs by mutableLongStateOf(SystemClock.elapsedRealtime())
    private val acker: AlertAcker = LogAlertAcker()
    private lateinit var vibrator: AlertVibrator
    private lateinit var lastAlert: LastAlertStore

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        vibrator = AlertVibrator(this)
        lastAlert = LastAlertStore(this)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        receive(intent)
        setContent { Content() }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        receive(intent)
    }

    override fun onDestroy() {
        vibrator.stop()
        super.onDestroy()
    }

    @Composable
    private fun Content() {
        // 기한이 지난 알림은 진동을 멈춘다
        LaunchedEffect(Unit) {
            while (true) {
                delay(TICK_MS)
                nowMs = SystemClock.elapsedRealtime()
                board = board.expireDue(nowMs)
            }
        }
        // 진동은 패턴이 바뀔 때만 다시 낸다. 같은 알림이 되풀이돼 와도 끊기지 않는다
        val vibrationKey = board.vibrating?.let { it.alert.eventId to it.alert.pattern }
        LaunchedEffect(vibrationKey) {
            if (vibrationKey == null) vibrator.stop() else vibrator.play(vibrationKey.second)
        }
        // 확인 대기 알림이 모두 없어지면 확인 표시를 잠깐 보이고 닫는다
        val nothingPending = board.pending.isEmpty()
        LaunchedEffect(nothingPending) {
            if (nothingPending) {
                delay(ACKED_SHOWN_MS)
                finishAndRemoveTask()
            }
        }

        val current = board.current
        if (current != null) {
            AlertScreen(current, board.others, nowMs) { acknowledge(current.alert.eventId) }
        } else {
            board.lastAcked?.ackedAtMs?.let { AckedScreen(it) }
        }
    }

    private fun receive(intent: Intent) {
        val alert = try {
            WatchAlert.fromFields(intent.alertFields())
        } catch (e: IllegalArgumentException) {
            // 알림 하나가 잘못돼도 화면은 그대로 둔다
            Log.w(TAG, "알림을 버린다: ${e.message}")
            return
        }
        // 기한은 단조 시계로 잰다. 시계가 뛰어도(NTP 보정) 기한이 흔들리지 않는다
        board = board.receive(alert, SystemClock.elapsedRealtime())
        lastAlert.save(System.currentTimeMillis())
    }

    private fun acknowledge(eventId: String) {
        val ackedAtMs = System.currentTimeMillis()
        board = board.acknowledge(eventId, ackedAtMs)
        acker.ack(eventId, ackedAtMs)
    }

    companion object {
        private const val TAG = "SimaiWatchAlert"

        /** 알림 화면을 띄우는 인텐트. EC2 연결 서비스(-337)와 debug 입구가 쓴다. */
        fun intentFor(context: Context, alert: WatchAlert): Intent =
            Intent(context, AlertActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                .putAlertFields(alert.toFields())
    }
}
