package com.ssafy.simai.watch

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.foundation.lazy.rememberScalingLazyListState
import androidx.wear.compose.material3.AppScaffold
import androidx.wear.compose.material3.ListHeader
import androidx.wear.compose.material3.MaterialTheme
import androidx.wear.compose.material3.ScreenScaffold
import androidx.wear.compose.material3.Text
import com.ssafy.simai.watch.config.WatchConfig
import com.ssafy.simai.watch.config.WatchConfigStore

/** 설정 상태 화면. EC2 연결·진동은 W4(S15P31A101-337)에서 붙인다. */
class MainActivity : ComponentActivity() {
    private var config by mutableStateOf<WatchConfig?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { StatusScreen(config) }
    }

    // adb 로 설정을 바꾼 뒤 앱으로 돌아오면 바로 보이게 화면에 올 때마다 다시 읽는다
    override fun onResume() {
        super.onResume()
        config = WatchConfigStore(this).load()
    }
}

@Composable
private fun StatusScreen(config: WatchConfig?) {
    MaterialTheme {
        AppScaffold {
            val listState = rememberScalingLazyListState()
            ScreenScaffold(scrollState = listState) { contentPadding ->
                ScalingLazyColumn(state = listState, contentPadding = contentPadding) {
                    item { ListHeader { Text("SIMAI 워치") } }
                    // 토큰 값은 화면에 보이지 않는다
                    item { StatusLine(if (config == null) "설정 안 됨" else "EC2 ${config.ec2Host}") }
                    item { StatusLine(if (config == null) "adb 로 주소·토큰을 넣는다" else "기기 토큰 설정됨") }
                    item { StatusLine("v${BuildConfig.VERSION_NAME}") }
                }
            }
        }
    }
}

@Composable
private fun StatusLine(text: String) {
    Text(text, textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth())
}
