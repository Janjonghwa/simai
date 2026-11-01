package com.ssafy.simai.watch.alert

import android.util.Log

/**
 * 워치에서 알림을 확인했다는 것을 EC2 로 보내는 곳(I4 ack). EC2 연결은 S15P31A101-337 에서 붙이고,
 * 그때 이 인터페이스를 실제 전송으로 바꾼다. 지금은 [LogAlertAcker] 가 기록만 남긴다.
 */
fun interface AlertAcker {
    fun ack(eventId: String, ackedAtMs: Long)
}

/** 보내지 않고 logcat 에 남긴다: `adb logcat -s SimaiWatchAck` */
class LogAlertAcker : AlertAcker {
    override fun ack(eventId: String, ackedAtMs: Long) {
        Log.i(TAG, "ack event_id=$eventId acked_at_ms=$ackedAtMs")
    }

    private companion object {
        const val TAG = "SimaiWatchAck"
    }
}
