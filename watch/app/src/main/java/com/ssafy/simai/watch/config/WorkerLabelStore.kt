package com.ssafy.simai.watch.config

import android.content.Context

/** 이름·구역을 앱 전용 저장소(SharedPreferences, MODE_PRIVATE)에 둔다. 이름과 구역은 늘 함께 저장하고 지운다. */
class WorkerLabelStore(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    /** 저장된 값이 없거나 지금 규칙에 맞지 않으면 null(기본 화면에 이름·구역 줄을 두지 않는다). 맞지 않는 값은 지운다. */
    fun load(): WorkerLabel? {
        val name = prefs.getString(KEY_NAME, null)
        val zone = prefs.getString(KEY_ZONE, null)
        if (name == null && zone == null) return null
        val label = if (name != null && zone != null) runCatching { WorkerLabel(name, zone) }.getOrNull() else null
        if (label == null) clear()
        return label
    }

    fun save(label: WorkerLabel): Boolean =
        prefs.edit().putString(KEY_NAME, label.name).putString(KEY_ZONE, label.zone).commit()

    fun clear(): Boolean = prefs.edit().clear().commit()

    private companion object {
        const val PREFS_NAME = "simai_watch_worker_label"
        const val KEY_NAME = "name"
        const val KEY_ZONE = "zone"
    }
}
