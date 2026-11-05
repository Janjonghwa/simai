package com.ssafy.simai.watch

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.collectAsState
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
import com.ssafy.simai.watch.service.AlertCenter
import com.ssafy.simai.watch.service.I4Service
import com.ssafy.simai.watch.ui.AmbientState
import com.ssafy.simai.watch.ui.StatusScreen

/**
 * 기본(대기) 화면. 손목을 내리면 어둡게 켜져 있는 화면(앰비언트 모드)이 된다.
 * 위험 알림 화면은 [AlertActivity], EC2 연결은 [I4Service] 가 맡고 여기서는 설정이 있으면 서비스를 깨운다.
 */
class MainActivity : ComponentActivity() {
    private var config by mutableStateOf<WatchConfig?>(null)
    private var lastAlertAtMs by mutableStateOf<Long?>(null)
    private var label by mutableStateOf<WorkerLabel?>(null)

    // null 이면 손목을 든 화면, 값이 있으면 어둡게 켜져 있는 화면
    private var ambient by mutableStateOf<AmbientState?>(null)

    // 앰비언트에서는 코루틴이 멈출 수 있어 시스템이 분마다 부르는 onUpdateAmbient 로 큰 시계를 다시 읽게 한다
    private var tickMs by mutableLongStateOf(System.currentTimeMillis())

    // 알림 권한(API 33 이상)이 없으면 연결 상태 알림·전체 화면 인텐트 알림이 보이지 않는다. 거부해도 연결·진동은 된다
    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}

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
        setContent {
            val status by AlertCenter.status.collectAsState()
            val binding by AlertCenter.binding.collectAsState()
            val board by AlertCenter.board.collectAsState()
            StatusScreen(config, lastAlertAtMs, label, ambient, tickMs, status, binding, alertPending = board.pending.isNotEmpty())
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    // adb 로 설정을 바꾼 뒤 앱으로 돌아오면 바로 보이게 화면에 올 때마다 다시 읽는다
    override fun onResume() {
        super.onResume()
        config = WatchConfigStore(this).load()
        lastAlertAtMs = LastAlertStore(this).load()
        label = WorkerLabelStore(this).load()
        if (config != null) I4Service.start(this)
    }

    private companion object {
        const val TAG = "SimaiWatchAmbient"
    }
}
