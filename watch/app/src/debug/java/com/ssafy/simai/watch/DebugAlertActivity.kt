package com.ssafy.simai.watch

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.widget.Toast
import com.ssafy.simai.watch.alert.AlertFields
import com.ssafy.simai.watch.alert.WatchAlert
import com.ssafy.simai.watch.service.I4Service
import java.util.UUID

/**
 * 가짜 알림을 띄우는 입구(debug 빌드 전용). EC2 없이 알림 화면·진동·확인 흐름을 시험한다. 알림은 연결 서비스로 넘겨 실제 알림과
 * 같은 길(보관·진동·VIBRATED 확인·화면)을 지난다. 확인은 연결이 있으면 EC2 로도 간다(EC2 는 모르는 알림이라 무시한다).
 *
 *   adb shell am start -n com.ssafy.simai.watch/.DebugAlertActivity --es severity CRITICAL --es word stop --es direction LEFT
 *
 * severity(CRITICAL·HIGH·CAUTION)만 필요하다. 나머지는 word=시험, ack_within_ms=30000, needs_check=false,
 * event_id·alert_msg_id=시각으로 만든 ID 로 채운다. vibration_pattern 은 주지 않으면 등급으로 정한다.
 * 같은 event_id 로 다시 부르면 같은 이벤트의 새 알림이고, 같은 alert_msg_id 로 다시 부르면 같은 알림이 한 번 더 온 것이다.
 */
class DebugAlertActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val now = System.currentTimeMillis()
        val fields = mapOf(
            // 계약의 uuid_v7 꼴을 흉내 낸다(가짜 EC2 가 확인을 검사할 때 형식에 걸리지 않게)
            AlertFields.EVENT_ID to "0192a6f0-7c1e-7b3a-9f2d-%012x".format(now),
            AlertFields.ALERT_MSG_ID to UUID.randomUUID().toString(),
            AlertFields.WORD to DEFAULT_WORD,
            AlertFields.SENT_AT to now.toString(),
            AlertFields.ACK_WITHIN_MS to DEFAULT_ACK_WITHIN_MS.toString(),
        ) + AlertFields.ALL.mapNotNull { name -> intent.getStringExtra(name)?.let { name to it } }
        try {
            WatchAlert.fromFields(fields) // 잘못된 값은 여기서 걸러 이유를 보여 준다
            I4Service.deliverAlert(this, fields)
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
