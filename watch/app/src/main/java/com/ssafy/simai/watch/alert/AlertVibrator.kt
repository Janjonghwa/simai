package com.ssafy.simai.watch.alert

import android.content.Context
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log

private const val NO_REPEAT = -1
private const val TAG = "SimaiWatchVibe"

/** [VibrationPattern] 을 워치 진동기로 낸다. VIBRATE 권한이 필요하다. */
class AlertVibrator(context: Context) {
    private val vibrator: Vibrator =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            context.getSystemService(VibratorManager::class.java).defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
        }

    /** 진행 중인 진동을 끊고 새 패턴을 낸다. 어떤 패턴을 냈는지 logcat 에 남긴다: `adb logcat -s SimaiWatchVibe` */
    fun play(pattern: VibrationPattern) {
        Log.i(TAG, "play ${pattern.name}")
        vibrator.vibrate(effectOf(pattern))
    }

    fun stop() {
        Log.i(TAG, "stop")
        vibrator.cancel()
    }

    private fun effectOf(pattern: VibrationPattern): VibrationEffect {
        val timings = pattern.timingsMs.toLongArray()
        val repeat = pattern.repeatFrom ?: NO_REPEAT
        // 세기를 못 바꾸는 진동기는 길이만 쓴다
        return if (vibrator.hasAmplitudeControl()) {
            VibrationEffect.createWaveform(timings, pattern.amplitudes.toIntArray(), repeat)
        } else {
            VibrationEffect.createWaveform(timings, repeat)
        }
    }
}
