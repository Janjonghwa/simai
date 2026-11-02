package com.ssafy.simai.watch

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.widget.Toast
import com.ssafy.simai.watch.config.WatchConfig
import com.ssafy.simai.watch.config.WatchConfigStore
import com.ssafy.simai.watch.service.I4Service

/**
 * adb 로 EC2 주소·기기 ID·기기 토큰을 넣는 입구(화면 없음). 긴 토큰을 워치 키보드로 칠 수 없어 이렇게 넣는다.
 *
 *   adb shell am start -n com.ssafy.simai.watch/.ConfigActivity --es ec2_base_url https://... --es device_id watch-01 --es device_token ...
 *   adb shell am start -n com.ssafy.simai.watch/.ConfigActivity --ez clear true
 *
 * adb shell 만 부를 수 있게 매니페스트에서 DUMP 권한을 요구한다(shell 은 갖고 서드파티 앱은 받을 수 없다).
 * 그래도 세 값은 함께 받을 때만 바꾼다. 주소만 바꿀 수 있으면 주소를 다른 서버로 돌려 저장된 토큰이 그리로 간다.
 * 저장하면 연결 서비스가 새 설정으로 다시 잇고, 지우면 서비스를 멈춘다.
 */
class ConfigActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val message = applyRequest(WatchConfigStore(this))
        // 토큰은 남기지 않는다. 결과 문장에는 입력값이 들어가지 않는다
        Log.i(TAG, message)
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
        finish()
    }

    private fun applyRequest(store: WatchConfigStore): String {
        val url = intent.getStringExtra(EXTRA_EC2_BASE_URL)
        val deviceId = intent.getStringExtra(EXTRA_DEVICE_ID)
        val token = intent.getStringExtra(EXTRA_DEVICE_TOKEN)
        // --ez clear true 가 맞지만 --es clear true 로 넣는 실수도 받는다
        val clear = intent.getBooleanExtra(EXTRA_CLEAR, false) || intent.getStringExtra(EXTRA_CLEAR) == "true"
        if (clear) {
            // 지우기와 새 값을 같이 주면 무엇을 원했는지 알 수 없어 아무것도 바꾸지 않는다
            if (url != null || deviceId != null || token != null) return "설정 거부: $EXTRA_CLEAR 와 주소·ID·토큰을 같이 넣지 않는다"
            if (!store.clear()) return "설정을 지우지 못했다"
            I4Service.stop(this)
            return "설정을 지웠다"
        }
        if (url == null || deviceId == null || token == null) {
            return "설정 거부: $EXTRA_EC2_BASE_URL·$EXTRA_DEVICE_ID·$EXTRA_DEVICE_TOKEN 을 함께 넣어야 한다"
        }
        val config = try {
            // 평문 http 는 debug 빌드에서 PC 의 가짜 EC2 에 붙을 때만 받는다
            WatchConfig.of(url, deviceId, token, allowPlainHttp = BuildConfig.DEBUG)
        } catch (e: IllegalArgumentException) {
            return "설정 거부: ${e.message}"
        }
        if (!store.save(config)) return "설정을 저장하지 못했다"
        I4Service.reload(this)
        return "설정을 저장했다: EC2 ${config.ec2Host}, 기기 ${config.deviceId}"
    }

    private companion object {
        const val TAG = "SimaiWatchConfig"
        const val EXTRA_EC2_BASE_URL = "ec2_base_url"
        const val EXTRA_DEVICE_ID = "device_id"
        const val EXTRA_DEVICE_TOKEN = "device_token"
        const val EXTRA_CLEAR = "clear"
    }
}
