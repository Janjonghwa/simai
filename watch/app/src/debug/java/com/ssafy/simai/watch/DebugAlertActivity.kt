package com.ssafy.simai.watch

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.widget.Toast
import com.ssafy.simai.watch.alert.AlertFields
import com.ssafy.simai.watch.alert.WatchAlert

/**
 * 가짜 알림을 띄우는 입구(debug 빌드 전용). EC2 연결(I4)이 없는 동안 알림 화면·진동을 시험한다.
 *
 *   adb shell am start -n com.ssafy.simai.watch/.DebugAlertActivity --es severity CRITICAL --es word stop --es direction LEFT
 *
 * severity(CRITICAL·HIGH·CAUTION)만 필요하다. 나머지는 word=시험, ack_within_ms=30000, needs_check=false,
 * event_id=debug-시각 으로 채운다. vibration 은 주지 않으면 등급으로 정한다.
 * 같은 event_id 로 다시 부르면 같은 알림이 한 번 더 온 것이다.
 */
class DebugAlertActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val now = System.currentTimeMillis()
        val fields = mapOf(
            AlertFields.EVENT_ID to "debug-$now",
            AlertFields.WORD to DEFAULT_WORD,
            AlertFields.SENT_AT_MS to now.toString(),
            AlertFields.ACK_WITHIN_MS to DEFAULT_ACK_WITHIN_MS.toString(),
        ) + AlertFields.ALL.mapNotNull { name -> intent.getStringExtra(name)?.let { name to it } }
        try {
            startActivity(AlertActivity.intentFor(this, WatchAlert.fromFields(fields)))
        } catch (e: IllegalArgumentException) {
            Log.w(TAG, "가짜 알림을 버린다: ${e.message}")
            Toast.makeText(this, "알림 거부: ${e.message}", Toast.LENGTH_LONG).show()
        }
        finish()
    }

    private companion object {
        const val TAG = "SimaiWatchDebugAlert"
        const val DEFAULT_WORD = "시험"
        const val DEFAULT_ACK_WITHIN_MS = 30_000L
    }
}
