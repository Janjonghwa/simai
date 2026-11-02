package com.ssafy.simai.watch.config

import android.content.Context
import com.ssafy.simai.watch.BuildConfig

/**
 * 설정을 앱 전용 저장소(SharedPreferences, MODE_PRIVATE)에 둔다. 백업은 매니페스트에서 끈다.
 * 주소·기기 ID·토큰은 늘 함께 저장하고 함께 지운다(ConfigActivity 참고).
 */
class WatchConfigStore(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    /**
     * 저장된 값이 없거나 지금 규칙에 맞지 않으면 null(설정 안 됨으로 보여 준다).
     * 규칙에 맞지 않는 값은 지운다: 화면은 "설정 안 됨" 인데 토큰만 기기에 남지 않게 한다.
     * (release 빌드에서는 debug 때 넣은 http 주소도 여기서 지워진다)
     */
    fun load(): WatchConfig? {
        val url = prefs.getString(KEY_EC2_BASE_URL, null)
        val deviceId = prefs.getString(KEY_DEVICE_ID, null)
        val token = prefs.getString(KEY_DEVICE_TOKEN, null)
        if (url == null && deviceId == null && token == null) return null
        val config = if (url != null && deviceId != null && token != null) {
            runCatching { WatchConfig.of(url, deviceId, token, allowPlainHttp = BuildConfig.DEBUG) }.getOrNull()
        } else {
            null
        }
        if (config == null) clear()
        return config
    }

    // ConfigActivity 가 바로 finish 하므로 apply 대신 commit 으로 쓰기를 끝낸다. 실패하면 false(저장소 가득 참 등)
    fun save(config: WatchConfig): Boolean =
        prefs.edit()
            .putString(KEY_EC2_BASE_URL, config.ec2BaseUrl)
            .putString(KEY_DEVICE_ID, config.deviceId)
            .putString(KEY_DEVICE_TOKEN, config.deviceToken)
            .commit()

    fun clear(): Boolean = prefs.edit().clear().commit()

    private companion object {
        const val PREFS_NAME = "simai_watch_config"
        const val KEY_EC2_BASE_URL = "ec2_base_url"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_DEVICE_TOKEN = "device_token"
    }
}
