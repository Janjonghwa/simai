package com.ssafy.simai.watch.gait

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.SystemClock
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

private const val TAG = "SimaiWatchGait"

/** 50 Hz 샘플 주기(µs)와 센서 묶음 전달 허용 지연(µs). 묶어 받으면 센서 허브가 CPU 를 덜 깨운다 */
private const val SAMPLE_PERIOD_US = 1_000_000 / SAMPLE_RATE_HZ
private const val BATCH_LATENCY_US = 1_000_000

/**
 * 가속도 센서를 50 Hz 로 받아 [GaitAnalyzer] 에 넣고, 2초마다 가장 최근 샘플로 끝나는 4초 창의 특징을 [onWindow] 로 넘긴다.
 * 센서 콜백과 창 계산은 모두 메인 스레드다(registerListener 에 핸들러를 주지 않는다). 연결돼 있는 동안만 돈다(I4Service).
 * 센서 시각(부팅 뒤 경과 ns)은 워치 벽시계(UTC ms)로 바꾼다: 계약의 모든 시각은 워치 시계다. 벽시계와 경과 시계의 차이는 시작할 때
 * 한 번 재서 쓴다(샘플마다 재면 NTP 보정 때 시각이 역행해 창이 멈추거나 뒤섞인다).
 */
class GaitSampler(context: Context, private val onWindow: (GaitFeatures) -> Unit) : SensorEventListener {
    private val manager: SensorManager? = context.getSystemService(SensorManager::class.java)

    // 깨우는(wake-up) 가속도계가 있으면 그것을 쓴다: 화면이 꺼져도 샘플이 온다
    private val sensor: Sensor? = manager?.getDefaultSensor(Sensor.TYPE_ACCELEROMETER, true) ?: manager?.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
    private val analyzer = GaitAnalyzer()
    private var job: Job? = null
    private var lastWindowEndMs: Long? = null
    private var wallMinusElapsedMs = 0L

    val available: Boolean get() = sensor != null
    val running: Boolean get() = job != null

    fun start(scope: CoroutineScope) {
        if (job != null) return
        val s = sensor
        if (manager == null || s == null) {
            Log.w(TAG, "가속도 센서가 없다: 걸음 특징을 보내지 않는다")
            return
        }
        analyzer.reset()
        lastWindowEndMs = null
        wallMinusElapsedMs = System.currentTimeMillis() - SystemClock.elapsedRealtime()
        if (!manager.registerListener(this, s, SAMPLE_PERIOD_US, BATCH_LATENCY_US)) {
            Log.w(TAG, "가속도 센서 등록 실패")
            return
        }
        Log.i(TAG, "가속도 수집 시작 ${s.name} wakeUp=${s.isWakeUpSensor}")
        job = scope.launch {
            while (isActive) {
                delay(HOP_MS)
                // 묶음 전달로 샘플이 늦게 오므로 지금이 아니라 마지막 샘플 시각으로 창을 닫는다. CPU 가 잠들었다 깨면 샘플이
                // 10초 넘게 한꺼번에 오므로 그 사이 창을 2초 간격으로 모두 만든다(늦더라도 빠지지 않게)
                val latest = analyzer.latestSampleMs ?: continue
                // 보관분(30초)보다 오래된 창은 어차피 비어 있으니 되짚기를 거기서부터 시작한다(오래 끊겼다 이어도 한 번에 수천 창을 만들지 않게)
                var end = (lastWindowEndMs?.let { it + HOP_MS } ?: latest).coerceAtLeast(latest - RETAIN_MS + WINDOW_MS)
                if (end > latest) continue
                while (end <= latest) {
                    analyzer.window(end)?.let(onWindow)
                    lastWindowEndMs = end
                    end += HOP_MS
                }
            }
        }
    }

    fun stop() {
        job?.cancel()
        job = null
        manager?.unregisterListener(this)
        analyzer.reset()
        lastWindowEndMs = null
    }

    override fun onSensorChanged(event: SensorEvent) {
        val wallMs = event.timestamp / 1_000_000 + wallMinusElapsedMs
        analyzer.add(wallMs, event.values[0].toDouble(), event.values[1].toDouble(), event.values[2].toDouble())
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit
}
