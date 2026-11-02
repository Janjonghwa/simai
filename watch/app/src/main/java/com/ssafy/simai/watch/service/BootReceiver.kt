package com.ssafy.simai.watch.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.ssafy.simai.watch.config.WatchConfigStore

/** 워치가 다시 켜지면 설정이 있을 때 EC2 연결을 다시 잇는다(BOOT_COMPLETED 는 포그라운드 서비스 시작이 허용된다). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        if (WatchConfigStore(context).load() != null) I4Service.start(context)
    }
}
