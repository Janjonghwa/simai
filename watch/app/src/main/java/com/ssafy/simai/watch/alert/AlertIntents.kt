package com.ssafy.simai.watch.alert

import android.content.Intent

/** 인텐트 extra 에서 알림 칸을 읽는다. 문자열이 아닌 extra 와 모르는 칸은 건너뛴다. */
fun Intent.alertFields(): Map<String, String> =
    AlertFields.ALL.mapNotNull { name -> getStringExtra(name)?.let { name to it } }.toMap()

/** 알림 칸을 인텐트 extra 로 싣는다. */
fun Intent.putAlertFields(fields: Map<String, String>): Intent {
    fields.forEach { (name, value) -> putExtra(name, value) }
    return this
}
