package com.ssafy.simai.watch.service

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import com.ssafy.simai.watch.BuildConfig
import com.ssafy.simai.watch.i4.Transport
import com.ssafy.simai.watch.i4.WatchStatus

/** 하트비트에 싣는 워치 상태(배터리·전송 경로·앱 판)를 Android 에서 읽는다. */
class DeviceStatus(private val context: Context) {

    fun status(): WatchStatus = WatchStatus(batteryPct(), transport(), BuildConfig.VERSION_NAME)

    /** 0~100. 모르면(에뮬레이터 등) 0 */
    fun batteryPct(): Int {
        val manager = context.getSystemService(BatteryManager::class.java) ?: return 0
        return manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY).coerceIn(0, 100)
    }

    /** 지금 쓰는 망. 휴대폰 경유(블루투스 프록시)는 Wear OS 가 TRANSPORT_BLUETOOTH 로 보인다 */
    fun transport(): Transport {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return Transport.UNKNOWN
        val caps = cm.activeNetwork?.let(cm::getNetworkCapabilities) ?: return Transport.UNKNOWN
        return when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> Transport.WIFI
            caps.hasTransport(NetworkCapabilities.TRANSPORT_BLUETOOTH) -> Transport.PHONE_BLUETOOTH
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> Transport.CELLULAR
            else -> Transport.UNKNOWN
        }
    }
}
