package com.ssafy.simai.watch.alert

import android.content.Context

/** 마지막으로 알림을 받은 시각(UTC ms). 상태 화면이 "마지막 알림" 을 보이는 데 쓴다. 알림 내용은 저장하지 않는다. */
class LastAlertStore(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun save(receivedAtMs: Long): Boolean = prefs.edit().putLong(KEY_RECEIVED_AT_MS, receivedAtMs).commit()

    fun load(): Long? = if (prefs.contains(KEY_RECEIVED_AT_MS)) prefs.getLong(KEY_RECEIVED_AT_MS, 0L) else null

    private companion object {
        const val FILE = "simai_watch_last_alert"
        const val KEY_RECEIVED_AT_MS = "received_at_ms"
    }
}
