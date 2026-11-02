package com.ssafy.simai.watch.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import com.ssafy.simai.watch.AlertActivity
import com.ssafy.simai.watch.MainActivity
import com.ssafy.simai.watch.alert.AlertSeverity
import com.ssafy.simai.watch.alert.WatchAlert

private const val TAG = "SimaiWatchNotify"

/**
 * 두 가지 알림: 포그라운드 서비스의 상시 알림(연결 상태)과, 위험 알림이 왔을 때 화면이 꺼져 있어도 [AlertActivity] 를 띄우는
 * 전체 화면 인텐트 알림. Android 10 이상은 앱이 앞에 없으면 서비스가 화면을 바로 띄울 수 없어 전체 화면 인텐트로 띄운다
 * (USE_FULL_SCREEN_INTENT). 앱이 앞에 있으면 startActivity 가 바로 띄운다.
 */
class AlertNotifier(private val context: Context) {
    private val manager = context.getSystemService(NotificationManager::class.java)

    init {
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_LINK, "EC2 연결", NotificationManager.IMPORTANCE_LOW).apply { setShowBadge(false) },
        )
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ALERT, "위험 알림", NotificationManager.IMPORTANCE_HIGH).apply {
                // 진동은 등급별 패턴으로 서비스가 직접 낸다
                enableVibration(false)
                setBypassDnd(true)
            },
        )
    }

    /** 포그라운드 서비스의 상시 알림 */
    fun link(text: String): Notification =
        Notification.Builder(context, CHANNEL_LINK)
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentTitle("SIMAI 워치")
            .setContentText(text)
            .setOngoing(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .setContentIntent(activity(Intent(context, MainActivity::class.java)))
            .build()

    fun updateLink(text: String) = manager.notify(LINK_ID, link(text))

    /** 위험 알림이 왔다: 전체 화면 인텐트 알림을 올리고 앞에 있으면 바로 화면을 띄운다 */
    fun showAlert(alert: WatchAlert) {
        val intent = AlertActivity.intent(context)
        val pending = activity(intent)
        val notification = Notification.Builder(context, CHANNEL_ALERT)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle("${label(alert.severity)} ${alert.word}")
            .setContentText(if (alert.needsCheck) "확인 필요" else "확인 단추를 누른다")
            .setCategory(Notification.CATEGORY_ALARM)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setOngoing(true)
            .setContentIntent(pending)
            .setFullScreenIntent(pending, true)
            .build()
        manager.notify(ALERT_ID, notification)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE && !manager.canUseFullScreenIntent()) {
            Log.w(TAG, "전체 화면 인텐트 권한이 없다: 화면이 꺼져 있으면 알림 화면이 뜨지 않는다(watch/README.md)")
        }
        // 앱이 앞에 있을 때는 이것이 바로 띄운다. 뒤에 있으면 시스템이 조용히 막고 전체 화면 인텐트가 띄운다
        try {
            context.startActivity(intent)
        } catch (e: RuntimeException) {
            Log.w(TAG, "알림 화면을 바로 띄우지 못했다: ${e.javaClass.simpleName}")
        }
    }

    /** 확인 대기 알림이 없어졌다 */
    fun clearAlert() = manager.cancel(ALERT_ID)

    private fun activity(intent: Intent): PendingIntent =
        PendingIntent.getActivity(context, 0, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    private fun label(severity: AlertSeverity): String = when (severity) {
        AlertSeverity.CRITICAL -> "위험"
        AlertSeverity.HIGH -> "경고"
        AlertSeverity.CAUTION -> "주의"
    }

    companion object {
        const val LINK_ID = 1
        const val ALERT_ID = 2
        private const val CHANNEL_LINK = "link"
        private const val CHANNEL_ALERT = "alert"
    }
}
