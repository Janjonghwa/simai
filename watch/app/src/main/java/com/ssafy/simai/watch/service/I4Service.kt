package com.ssafy.simai.watch.service

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import com.ssafy.simai.watch.alert.AckKind
import com.ssafy.simai.watch.alert.AckRecord
import com.ssafy.simai.watch.alert.AlertBoard
import com.ssafy.simai.watch.alert.AlertPhase
import com.ssafy.simai.watch.alert.AlertVibrator
import com.ssafy.simai.watch.alert.LastAlertStore
import com.ssafy.simai.watch.alert.VibrationPattern
import com.ssafy.simai.watch.alert.WatchAlert
import com.ssafy.simai.watch.alert.alertFields
import com.ssafy.simai.watch.alert.putAlertFields
import com.ssafy.simai.watch.config.WatchConfig
import com.ssafy.simai.watch.config.WatchConfigStore
import com.ssafy.simai.watch.gait.GaitFeatures
import com.ssafy.simai.watch.gait.GaitSampler
import com.ssafy.simai.watch.i4.Backoff
import com.ssafy.simai.watch.i4.CLOSE_RECONFIGURED
import com.ssafy.simai.watch.i4.CLOSE_STOPPING
import com.ssafy.simai.watch.i4.ConnectionStatus
import com.ssafy.simai.watch.i4.GaitSummary
import com.ssafy.simai.watch.i4.HeartbeatState
import com.ssafy.simai.watch.i4.I4Codec
import com.ssafy.simai.watch.i4.I4Connection
import com.ssafy.simai.watch.i4.Incoming
import com.ssafy.simai.watch.i4.LinkState
import com.ssafy.simai.watch.i4.PendingAcks
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.WebSocket

private const val TAG = "SimaiWatchI4"

/** 기한을 살피는 간격(ms). 확인 대기 알림이 있을 때만 돈다 */
private const val EXPIRE_TICK_MS = 200L

/** 알림을 받아 진동·ACK 를 끝낼 때까지 깨어 있는 시간 */
private const val ALERT_WAKE_MS = 10_000L

/** 받은 글이 알림인지 파싱 전에 어림하는 표시. 파싱은 메인 스레드에서 하므로 그 전에 깨어 있을지만 정한다(잘못 잡아도 10초 뒤 풀린다) */
private const val ALERT_MARKER = "\"watch_alert\""

/** 보낸 확인을 기억하는 수. 같은 알림이 다시 오면 되돌려 주고, 다시 이어지면 최근 것을 다시 보낸다 */
private const val REMEMBERED_ACKS = 64

/** 다시 이어졌을 때 이 시간 안의 확인은 다시 보낸다(ws.send 의 true 는 큐에 넣었다는 뜻이지 닿았다는 뜻이 아니다). 확인 기한 상한과 같다 */
private const val RESEND_ACK_WINDOW_MS = 10 * 60 * 1000L

/**
 * EC2 와의 I4 연결을 화면이 꺼져도 유지하는 포그라운드 서비스(S15P31A101-337, 부록 C.6).
 *
 * - 연결: wss://EC2/ws/watch, 기기 토큰. 끊기면 1·2·4…30초 간격으로 다시 잇는다(첫 echo 를 받아야 대기를 처음으로 되돌린다).
 *   연결 중 wake lock 은 쥐지 않는다: 실기기에서 CPU 수면(약 20초 주기)을 막지 못하면서 배터리 통계에는 앱 소모의 대부분으로 집계됐다(설계 문서의 측정).
 *   하트비트가 그만큼 늦어도 EC2 가 견디게 끊김 판정을 넉넉히 둔다(-336). 알림을 받을 때만 진동·확인이 끝나도록 잠깐 쥔다
 * - 하트비트: EC2 가 정한 주기(기본 10초)로 보내고, 내 하트비트 3회에 EC2 답(echo)이 없으면 연결을 끊고 다시 잇는다
 * - 알림: 받으면 바로 진동하고 VIBRATED 확인을 보낸 뒤 알림 화면을 띄운다. 작업자가 확인하면 CONFIRMED 를 보낸다.
 *   끊긴 동안의 확인은 쌓아 뒀다가 다시 이어지면 보낸다
 * - 알림 보관([AlertBoard])·진동·확인은 모두 여기서 하고, 화면은 [AlertCenter] 를 관찰한다
 * - 걸음 특징(S15P31A101-339): 연결돼 있는 동안 가속도를 받아 2초마다 gait_features 를 보낸다(끊기면 센서도 멈춘다)
 *
 * 모든 상태는 메인 스레드에서만 만진다(OkHttp 콜백은 [post] 로 옮긴다).
 */
class I4Service : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val connection = I4Connection()
    private val heartbeat = HeartbeatState()
    private val backoff = Backoff()
    private val pendingAcks = PendingAcks()
    private val sentAcks = LinkedHashMap<Pair<String, AckKind>, AckRecord>()
    private var loaded = false

    private var config: WatchConfig? = null
    private var codec: I4Codec? = null
    private var socket: WebSocket? = null
    private var connectJob: Job? = null
    private var heartbeatJob: Job? = null
    private var expireJob: Job? = null
    private var vibrationKey: Pair<String, VibrationPattern>? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    private lateinit var vibrator: AlertVibrator
    private lateinit var notifier: AlertNotifier
    private lateinit var lastAlert: LastAlertStore
    private lateinit var device: DeviceStatus
    private lateinit var wakeLock: PowerManager.WakeLock
    private lateinit var gait: GaitSampler

    private var board = AlertBoard()
        set(value) {
            field = value
            AlertCenter.publishBoard(value)
        }

    private var status = ConnectionStatus()
        set(value) {
            field = value
            AlertCenter.publishStatus(value)
        }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        vibrator = AlertVibrator(this)
        notifier = AlertNotifier(this)
        lastAlert = LastAlertStore(this)
        device = DeviceStatus(this)
        gait = GaitSampler(this, ::sendGait)
        val power = getSystemService(PowerManager::class.java)
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "simai:alert")
        startForeground(AlertNotifier.LINK_ID, notifier.link("연결 준비"), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        AlertCenter.attach(this)
        holdNetwork()
    }

    // 처음 뜰 때는 onStartCommand 의 인텐트가 어떤 것이든 한 번만 읽는다(RELOAD 로 처음 뜨면 두 번 잇지 않게)
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_RELOAD -> reload()
            ACTION_DELIVER_ALERT -> {
                if (!loaded) reload()
                deliverFromIntent(intent)
            }
            else -> if (!loaded) reload()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        gait.stop()
        socket?.close(CLOSE_STOPPING, "stopping")
        socket = null
        vibrator.stop()
        notifier.clearAlert()
        networkCallback?.let { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) }
        if (wakeLock.isHeld) wakeLock.release()
        AlertCenter.detach(this)
        super.onDestroy()
    }

    // ---- 연결 ----

    /** 설정을 다시 읽고 연결을 처음부터 다시 한다 */
    private fun reload() {
        loaded = true
        connectJob?.cancel()
        heartbeatJob?.cancel()
        gait.stop()
        // 아직 안 열린 소켓은 close 가 핸드셰이크 뒤에야 나가므로 바로 끊는다
        socket?.let { if (status.state == LinkState.CONNECTED) it.close(CLOSE_RECONFIGURED, "reconfigured") else it.cancel() }
        socket = null
        config = WatchConfigStore(this).load()
        codec = config?.let { I4Codec(it.deviceId) }
        if (config == null) {
            status = ConnectionStatus(LinkState.NOT_CONFIGURED)
            notifier.updateLink("설정 안 됨")
            return
        }
        backoff.reset()
        connectJob = scope.launch { connectLoop() }
    }

    private suspend fun connectLoop() {
        while (scope.isActive) {
            val cfg = config ?: return
            val closed = CompletableDeferred<Unit>()
            heartbeat.reset()
            status = status.copy(state = LinkState.CONNECTING)
            lateinit var ws: WebSocket
            val listener = object : I4Connection.Listener {
                override fun onOpen() = post { if (socket === ws) onOpened(ws) }
                override fun onText(text: String) {
                    // 알림이면 메인 스레드로 넘어가기 전에 잡는다: 절전 중 OkHttp 스레드에서 메인으로 넘어가는 사이에 CPU 가 다시 잠들 수 있다(WakeLock 은 스레드 안전)
                    if (text.contains(ALERT_MARKER)) wakeLock.acquire(ALERT_WAKE_MS)
                    post { if (socket === ws) onTextReceived(text) }
                }
                override fun onClosed(code: Int, reason: String) = post {
                    if (socket === ws) onDropped("닫힘 $code $reason")
                    closed.complete(Unit)
                }
                override fun onFailure(t: Throwable) = post {
                    if (socket === ws) onDropped("${t.javaClass.simpleName}: ${t.message}")
                    closed.complete(Unit)
                }
            }
            try {
                ws = connection.open(cfg.wsUrl, cfg.deviceToken, listener)
            } catch (e: RuntimeException) {
                // OkHttp 가 주소를 거부하면(java.net.URI 가 통과시킨 포트·문자 등) 여기서 던진다. 루프가 죽지 않게 받아 다시 센다
                onDropped("주소 거부 ${e.javaClass.simpleName}: ${e.message}")
                delay(backoff.next())
                continue
            }
            socket = ws
            closed.await()
            if (socket === ws) socket = null
            heartbeatJob?.cancel()
            delay(backoff.next())
        }
    }

    private fun onOpened(ws: WebSocket) {
        status = status.copy(state = LinkState.CONNECTED, lastFailure = null)
        notifier.updateLink("EC2 연결됨")
        resendAcks(ws)
        gait.start(scope)
        heartbeatJob?.cancel()
        heartbeatJob = scope.launch {
            while (isActive) {
                if (heartbeat.isDead) {
                    Log.w(TAG, "EC2 답 없음 ${heartbeat.unanswered}회: 연결을 끊고 다시 잇는다")
                    status = status.copy(lastFailure = "EC2 답 없음 ${heartbeat.unanswered}회")
                    ws.cancel()
                    return@launch
                }
                sendHeartbeat(ws)
                delay(heartbeat.intervalMs)
            }
        }
    }

    private fun onDropped(reason: String) {
        heartbeatJob?.cancel()
        gait.stop()
        // 내가 끊은 이유(답 없음)를 적어 뒀으면 그것을 남긴다
        status = status.copy(state = LinkState.DISCONNECTED, lastFailure = status.lastFailure ?: reason)
        notifier.updateLink("EC2 연결 끊김")
        Log.w(TAG, "연결 끊김: $reason")
    }

    private fun sendHeartbeat(ws: WebSocket) {
        val c = codec ?: return
        val now = System.currentTimeMillis()
        val out = heartbeat.nextOutgoing(now)
        val watch = device.status()
        status = status.copy(batteryPct = watch.batteryPct, transport = watch.transport)
        if (!ws.send(c.heartbeat(out.seq, out.echo, watch, now))) Log.w(TAG, "하트비트를 보내지 못했다 seq=${out.seq}")
    }

    private fun onTextReceived(text: String) {
        val c = codec ?: return
        val incoming = try {
            c.parse(text)
        } catch (e: IllegalArgumentException) {
            Log.w(TAG, "메시지를 버린다: ${e.message}")
            return
        }
        when (incoming) {
            is Incoming.Alert -> receive(incoming.alert)
            is Incoming.Heartbeat -> {
                val now = System.currentTimeMillis()
                heartbeat.onEc2Heartbeat(incoming, now)
                // 연결이 실제로 살아 있다는 첫 증거(핸드셰이크 뒤 바로 닫히는 서버에 1초마다 붙지 않게 여기서 되돌린다)
                if (heartbeat.answered) backoff.reset()
                status = status.copy(lastEc2AtMs = now, clockOffsetMs = heartbeat.clockOffsetMs)
            }
            is Incoming.Unknown -> Log.i(TAG, "모르는 메시지를 무시한다: ${incoming.msgType}")
        }
    }

    // ---- 걸음 특징 ----

    /** 2초마다 한 창. 연결이 없으면 버린다(쌓아 두지 않는다: 대조는 지금 움직임이 중요하다). 보낸 창만 상태 화면에 올린다 */
    private fun sendGait(features: GaitFeatures) {
        val ws = socket
        val c = codec
        if (status.state != LinkState.CONNECTED || ws == null || c == null) return
        val text = try {
            c.gaitFeatures(features, heartbeat.clockOffsetMs, System.currentTimeMillis())
        } catch (e: RuntimeException) {
            // 값이 수가 아니면(org.json 은 NaN 을 거부) 그 창만 버리고 서비스는 산다
            Log.w(TAG, "걸음 특징을 만들지 못했다: ${e.javaClass.simpleName}")
            return
        }
        if (!ws.send(text)) {
            Log.w(TAG, "걸음 특징을 보내지 못했다")
            return
        }
        status = status.copy(
            lastGait = GaitSummary(features.windowEndMs, features.stepTimesMs.size, features.rms, features.peakFreqHz, features.moving),
        )
    }

    // ---- 알림 ----

    /**
     * 알림을 받았다(EC2 또는 debug 입구). 같은 msg_id 가 다시 오면 진동하지 않고 확인만 다시 보낸다.
     * VIBRATED 는 그 이벤트가 실제로 울리는 상태(ACTIVE)일 때만 보낸다: 확인한 이벤트의 같은 알림이 되풀이돼 오면 보내지 않는다
     * (보내면 EC2 가 격상을 풀어 버린다). 확인한 뒤 새 알림(다른 msg_id, 등급 상향)이 오면 보드가 다시 울린다
     */
    private fun receive(alert: WatchAlert) {
        sentAcks[alert.alertMsgId to AckKind.VIBRATED]?.let {
            sendAck(it)
            return
        }
        if (!wakeLock.isHeld) wakeLock.acquire(ALERT_WAKE_MS) // debug 입구는 소켓을 거치지 않아 여기서 잡는다
        val now = System.currentTimeMillis()
        board = board.receive(alert, SystemClock.elapsedRealtime())
        lastAlert.save(now)
        syncVibration()
        ensureExpireTicker()
        val tracked = board.items.firstOrNull { it.alert.eventId == alert.eventId } ?: return
        if (tracked.phase != AlertPhase.ACTIVE) {
            Log.i(TAG, "확인한 이벤트의 알림이 다시 왔다: 울리지 않고 확인도 보내지 않는다 event_id=${alert.eventId}")
            return
        }
        sendAck(AckRecord(alert.alertMsgId, alert.eventId, alert.eventSequence, AckKind.VIBRATED, ackedAtMs = now, receivedAtMs = now))
        notifier.showAlert(alert)
    }

    /** 작업자가 확인 단추를 눌렀다. 기한이 지난 알림도 받는다 */
    fun acknowledge(eventId: String) {
        val tracked = board.items.firstOrNull { it.alert.eventId == eventId && it.phase != AlertPhase.ACKED } ?: return
        val now = System.currentTimeMillis()
        board = board.acknowledge(eventId, now)
        syncVibration()
        val alert = tracked.alert
        sendAck(AckRecord(alert.alertMsgId, eventId, alert.eventSequence, AckKind.CONFIRMED, ackedAtMs = now, receivedAtMs = null))
        if (board.pending.isEmpty()) notifier.clearAlert()
    }

    private fun deliverFromIntent(intent: Intent) {
        val alert = try {
            WatchAlert.fromFields(intent.alertFields())
        } catch (e: IllegalArgumentException) {
            Log.w(TAG, "알림을 버린다: ${e.message}")
            return
        }
        receive(alert)
    }

    /** 진동은 패턴이 바뀔 때만 다시 낸다. 같은 알림이 되풀이돼 와도 끊기지 않는다 */
    private fun syncVibration() {
        val key = board.vibrating?.let { it.alert.eventId to it.alert.pattern }
        if (key == vibrationKey) return
        vibrationKey = key
        if (key == null) vibrator.stop() else vibrator.play(key.second)
    }

    /** 확인 대기 알림이 있는 동안만 기한을 살핀다. 기한이 지나면 진동만 멈춘다(화면·확인 단추는 남는다) */
    private fun ensureExpireTicker() {
        if (expireJob?.isActive == true) return
        expireJob = scope.launch {
            while (board.vibrating != null) {
                delay(EXPIRE_TICK_MS)
                board = board.expireDue(SystemClock.elapsedRealtime())
                syncVibration()
            }
        }
    }

    // ---- 확인 보내기 ----

    private fun sendAck(record: AckRecord) {
        sentAcks.remove(record.key)
        sentAcks[record.key] = record
        while (sentAcks.size > REMEMBERED_ACKS) sentAcks.remove(sentAcks.keys.first())
        val ws = socket
        val c = codec
        val sent = status.state == LinkState.CONNECTED && ws != null && c != null && ws.send(c.ack(record, System.currentTimeMillis()))
        if (!sent) pendingAcks.add(record)
        Log.i(TAG, "ack ${record.kind} event_id=${record.eventId} ${if (sent) "보냄" else "보류 ${pendingAcks.size}건"}")
    }

    /** 다시 이어졌다: 보류한 확인과, 끊기기 전에 큐에만 넣었을 수 있는 최근 확인을 다시 보낸다(EC2 가 (alert_msg_id, kind) 로 중복을 뺀다) */
    private fun resendAcks(ws: WebSocket) {
        val c = codec ?: return
        val now = System.currentTimeMillis()
        val recent = sentAcks.values.filter { it.ackedAtMs >= now - RESEND_ACK_WINDOW_MS }
        for (record in (pendingAcks.drain() + recent).distinctBy { it.key }) {
            if (!ws.send(c.ack(record, now))) pendingAcks.add(record)
        }
    }

    // ---- 보조 ----

    private fun post(block: () -> Unit) {
        scope.launch { block() }
    }

    /** 화면이 꺼져도 망(Wi-Fi 또는 휴대폰 경유)을 붙들어 둔다. 요청이 살아 있는 동안 시스템이 망을 내리지 않는다 */
    private fun holdNetwork() {
        val cm = getSystemService(ConnectivityManager::class.java) ?: return
        val callback = object : ConnectivityManager.NetworkCallback() {}
        try {
            cm.requestNetwork(NetworkRequest.Builder().addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).build(), callback)
            networkCallback = callback
        } catch (e: SecurityException) {
            Log.w(TAG, "망을 붙들지 못했다: ${e.message}")
        }
    }

    companion object {
        private const val ACTION_RELOAD = "com.ssafy.simai.watch.RELOAD"
        private const val ACTION_DELIVER_ALERT = "com.ssafy.simai.watch.DELIVER_ALERT"

        /** 설정이 있으면 연결을 시작한다(이미 돌고 있으면 그대로) */
        fun start(context: Context) {
            context.startForegroundService(Intent(context, I4Service::class.java))
        }

        /** 설정이 바뀌었다: 다시 읽고 다시 잇는다(돌고 있지 않으면 시작한다) */
        fun reload(context: Context) {
            context.startForegroundService(Intent(context, I4Service::class.java).setAction(ACTION_RELOAD))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, I4Service::class.java))
        }

        /** debug 입구가 가짜 알림을 넘긴다. 칸 이름은 [com.ssafy.simai.watch.alert.AlertFields] */
        fun deliverAlert(context: Context, fields: Map<String, String>) {
            context.startForegroundService(
                Intent(context, I4Service::class.java).setAction(ACTION_DELIVER_ALERT).putAlertFields(fields),
            )
        }
    }
}
