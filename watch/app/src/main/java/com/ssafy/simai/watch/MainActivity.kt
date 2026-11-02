package com.ssafy.simai.watch

import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.wear.ambient.AmbientLifecycleObserver
import com.ssafy.simai.watch.alert.LastAlertStore
import com.ssafy.simai.watch.config.WatchConfig
import com.ssafy.simai.watch.config.WatchConfigStore
import com.ssafy.simai.watch.config.WorkerLabel
import com.ssafy.simai.watch.config.WorkerLabelStore
import com.ssafy.simai.watch.ui.AmbientState
import com.ssafy.simai.watch.ui.StatusScreen

/**
 * 기본(대기) 화면. 손목을 내리면 어둡게 켜져 있는 화면(앰비언트 모드)이 된다.
 * 위험 알림 화면은 [AlertActivity], EC2 연결은 W4(S15P31A101-337)에서 붙인다.
 */
class MainActivity : ComponentActivity() {
    private var config by mutableStateOf<WatchConfig?>(null)
    private var lastAlertAtMs by mutableStateOf<Long?>(null)
    private var label by mutableStateOf<WorkerLabel?>(null)

    // null 이면 손목을 든 화면, 값이 있으면 어둡게 켜져 있는 화면
    private var ambient by mutableStateOf<AmbientState?>(null)

    // 앰비언트에서는 코루틴이 멈출 수 있어 시스템이 분마다 부르는 onUpdateAmbient 로 큰 시계를 다시 읽게 한다
    private var tickMs by mutableLongStateOf(System.currentTimeMillis())

    private val ambientCallback = object : AmbientLifecycleObserver.AmbientLifecycleCallback {
        override fun onEnterAmbient(ambientDetails: AmbientLifecycleObserver.AmbientDetails) {
            ambient = AmbientState(
                lowBit = ambientDetails.deviceHasLowBitAmbient,
                burnInProtection = ambientDetails.burnInProtectionRequired,
            )
            tickMs = System.currentTimeMillis()
            Log.i(TAG, "어둡게 켜짐: $ambient")
        }

        override fun onExitAmbient() {
            ambient = null
            tickMs = System.currentTimeMillis()
            Log.i(TAG, "밝게 켜짐")
        }

        override fun onUpdateAmbient() {
            tickMs = System.currentTimeMillis()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        lifecycle.addObserver(AmbientLifecycleObserver(this, ambientCallback))
        setContent { StatusScreen(config, lastAlertAtMs, label, ambient, tickMs) }
    }

    // adb 로 설정을 바꾼 뒤 앱으로 돌아오면 바로 보이게 화면에 올 때마다 다시 읽는다
    override fun onResume() {
        super.onResume()
        config = WatchConfigStore(this).load()
        lastAlertAtMs = LastAlertStore(this).load()
        label = WorkerLabelStore(this).load()
    }

    private companion object {
        const val TAG = "SimaiWatchAmbient"
    }
}
