package com.ssafy.simai.watch

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.ssafy.simai.watch.alert.LastAlertStore
import com.ssafy.simai.watch.config.WatchConfig
import com.ssafy.simai.watch.config.WatchConfigStore
import com.ssafy.simai.watch.ui.StatusScreen

/** 상태 화면. 위험 알림 화면은 [AlertActivity], EC2 연결은 W4(S15P31A101-337)에서 붙인다. */
class MainActivity : ComponentActivity() {
    private var config by mutableStateOf<WatchConfig?>(null)
    private var lastAlertAtMs by mutableStateOf<Long?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { StatusScreen(config, lastAlertAtMs) }
    }

    // adb 로 설정을 바꾼 뒤 앱으로 돌아오면 바로 보이게 화면에 올 때마다 다시 읽는다
    override fun onResume() {
        super.onResume()
        config = WatchConfigStore(this).load()
        lastAlertAtMs = LastAlertStore(this).load()
    }
}
