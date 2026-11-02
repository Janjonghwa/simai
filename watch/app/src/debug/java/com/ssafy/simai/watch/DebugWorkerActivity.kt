package com.ssafy.simai.watch

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.widget.Toast
import com.ssafy.simai.watch.config.WorkerLabel
import com.ssafy.simai.watch.config.WorkerLabelStore

/**
 * 기본 화면의 이름·구역을 넣는 입구(debug 빌드 전용). 이름·구역을 줄 서버 계약(-334)이 없는 동안 쓴다.
 *
 *   adb shell am start -n com.ssafy.simai.watch/.DebugWorkerActivity --es name 김반장 --es zone "3층 A구역"
 *   adb shell am start -n com.ssafy.simai.watch/.DebugWorkerActivity --ez clear true
 *
 * 이름과 구역은 함께 줄 때만 바꾼다.
 */
class DebugWorkerActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val message = apply(WorkerLabelStore(this))
        Log.i(TAG, message)
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
        finish()
    }

    private fun apply(store: WorkerLabelStore): String {
        if (intent.getBooleanExtra(EXTRA_CLEAR, false)) {
            return if (store.clear()) "이름·구역을 지웠다" else "이름·구역을 지우지 못했다"
        }
        val name = intent.getStringExtra(EXTRA_NAME)
        val zone = intent.getStringExtra(EXTRA_ZONE)
        if (name == null || zone == null) return "거부: $EXTRA_NAME 과 $EXTRA_ZONE 을 함께 넣어야 한다"
        val label = try {
            WorkerLabel(name.trim(), zone.trim())
        } catch (e: IllegalArgumentException) {
            return "거부: ${e.message}"
        }
        return if (store.save(label)) "이름·구역을 저장했다" else "이름·구역을 저장하지 못했다"
    }

    private companion object {
        const val TAG = "SimaiWatchDebugWorker"
        const val EXTRA_NAME = "name"
        const val EXTRA_ZONE = "zone"
        const val EXTRA_CLEAR = "clear"
    }
}
