package com.ssafy.simai.watch.i4

import com.ssafy.simai.watch.alert.AckRecord
import com.ssafy.simai.watch.alert.AlertFields
import com.ssafy.simai.watch.alert.WatchAlert
import com.ssafy.simai.watch.binding.BindingMethod
import com.ssafy.simai.watch.binding.BindingNotice
import com.ssafy.simai.watch.binding.BindingState
import com.ssafy.simai.watch.gait.GaitFeatures
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.util.UUID

/** I4 메시지의 schema_version(contracts/messages/watch_*.schema.json 1.0.0) */
const val I4_SCHEMA_VERSION = "1.0.0"

/** EC2 가 보내는 메시지의 봉투 source_id */
const val EC2_SOURCE_ID = "ec2"

/** EC2 에서 받은 메시지 한 건 */
sealed interface Incoming {
    /** watch_alert. [alert] 의 alertMsgId 가 봉투 msg_id 다 */
    data class Alert(val alert: WatchAlert) : Incoming

    /**
     * EC2 → 워치 watch_heartbeat. echo 는 계약상 필수다.
     * @property sentAtMs 봉투 sent_at(EC2 시계) — 시계 차이 계산의 t3
     * @property echoReceivedAtMs EC2 가 워치의 [echoSeq] 를 받은 EC2 시각 — t2
     */
    data class Heartbeat(val seq: Long, val sentAtMs: Long, val echoSeq: Long, val echoReceivedAtMs: Long, val intervalMs: Long?) : Incoming

    /** watch_binding_notice(S15P31A101-510): 이 워치에 사람 배정이 생겼거나 풀렸다 */
    data class Binding(val notice: BindingNotice) : Incoming

    /**
     * watch_alert_clear(S15P31A101-634): [eventId] 의 이벤트가 해소됐으니 그 알림을 지운다. 확인(ACK)이 아니고 답하지 않는다.
     * [reason] 은 지금 RESOLVED 뿐이고 새 값이 생길 수 있어(마이너) 모르는 값도 받아 지운다
     */
    data class Clear(val eventId: String, val reason: String) : Incoming

    /** 모르는 msg_type. 수신 측은 모르는 것을 무시한다(명세 12.1) */
    data class Unknown(val msgType: String) : Incoming
}

/** 워치 → EC2 하트비트에 싣는 워치 상태(계약 watch_heartbeat.payload.watch) */
data class WatchStatus(val batteryPct: Int, val transport: Transport, val appVersion: String)

/** EC2 까지 가는 길(계약 transport). 지연 차이 기록용 */
enum class Transport { WIFI, PHONE_BLUETOOTH, CELLULAR, UNKNOWN }

/** 상대에게서 마지막으로 받은 하트비트: 그 seq 와 받은 내 시계 시각(계약 echo) */
data class Echo(val seq: Long, val receivedAtMs: Long)

/**
 * I4 메시지를 읽고 만든다. 봉투는 명세 12.1. 잘못된 메시지는 IllegalArgumentException 이고 부르는 쪽이 버린다.
 * JSON 은 Android 에 있는 org.json 을 쓴다(의존성 없음). 토큰·사람 이름은 어디에도 싣지 않는다.
 *
 * @param sourceId 봉투 source_id = 워치 기기 ID(device.device_id, watch-01 꼴)
 */
class I4Codec(private val sourceId: String) {

    fun parse(text: String): Incoming {
        val doc = try {
            JSONObject(text)
        } catch (e: JSONException) {
            throw IllegalArgumentException("JSON 이 아니다")
        }
        val msgType = doc.optString("msg_type", "")
        require(msgType.isNotEmpty()) { "msg_type 이 없다" }
        val payload = doc.optJSONObject("payload") ?: throw IllegalArgumentException("payload 가 없다")
        return when (msgType) {
            "watch_alert" -> Incoming.Alert(alertOf(doc, payload))
            "watch_heartbeat" -> heartbeatOf(doc, payload)
            "watch_binding_notice" -> Incoming.Binding(bindingOf(payload))
            "watch_alert_clear" -> Incoming.Clear(requiredString(payload, "event_id"), requiredString(payload, "reason"))
            else -> Incoming.Unknown(msgType)
        }
    }

    /** 워치 → EC2 하트비트(계약 watch_heartbeat: watch 블록 필수, interval_ms 없음) */
    fun heartbeat(seq: Long, echo: Echo?, status: WatchStatus, nowMs: Long): String {
        val payload = JSONObject().put("heartbeat_seq", seq)
        if (echo != null) payload.put("echo", JSONObject().put("seq", echo.seq).put("received_at", echo.receivedAtMs))
        payload.put(
            "watch",
            JSONObject().put("battery_pct", status.batteryPct).put("transport", status.transport.name).put("app_version", status.appVersion),
        )
        return envelope("watch_heartbeat", payload, nowMs).toString()
    }

    /** 워치 → EC2 확인(계약 watch_ack) */
    fun ack(record: AckRecord, nowMs: Long): String {
        val payload = JSONObject()
            .put("event_id", record.eventId)
            .put("alert_msg_id", record.alertMsgId)
            .put("kind", record.kind.name)
            .put("acked_at", record.ackedAtMs)
        record.eventSequence?.let { payload.put("event_sequence", it) }
        record.receivedAtMs?.let { payload.put("received_at", it) }
        return envelope("watch_ack", payload, nowMs).toString()
    }

    /** 워치 → EC2 걸음 특징 한 창(계약 gait_features). 시각은 워치 시계, 시계 차이는 모르면 null 로 싣는다 */
    fun gaitFeatures(features: GaitFeatures, clockOffsetMs: Long?, nowMs: Long): String {
        val band = JSONObject()
            .put("rate_hz", features.bandRateHz)
            .put("values", JSONArray(features.bandValues.map { round3(it) }))
            .put("rms", round3(features.rms))
            .put("peak_freq_hz", features.peakFreqHz?.let { round3(it) } ?: JSONObject.NULL)
        val payload = JSONObject()
            .put("clock_basis", "WATCH")
            .put("clock_offset_ms", clockOffsetMs ?: JSONObject.NULL)
            .put("window_start_ms", features.windowStartMs)
            .put("window_end_ms", features.windowEndMs)
            .put("sample_rate_hz", features.sampleRateHz)
            .put("step_times_ms", JSONArray(features.stepTimesMs))
        if (features.transitions.isNotEmpty()) {
            payload.put("motion_transitions", JSONArray(features.transitions.map { JSONObject().put("at_ms", it.atMs).put("kind", it.kind.name) }))
        }
        payload.put("band_magnitude", band)
        return envelope("gait_features", payload, nowMs).toString()
    }

    /** 메시지를 작게: 소수 셋째 자리까지 */
    private fun round3(x: Double): Double = Math.round(x * 1000) / 1000.0

    private fun envelope(msgType: String, payload: JSONObject, nowMs: Long): JSONObject =
        JSONObject()
            .put("schema_version", I4_SCHEMA_VERSION)
            .put("msg_type", msgType)
            .put("msg_id", UUID.randomUUID().toString())
            .put("sent_at", nowMs)
            .put("source_id", sourceId)
            .put("payload", payload)

    private fun alertOf(doc: JSONObject, payload: JSONObject): WatchAlert {
        val fields = buildMap {
            put(AlertFields.ALERT_MSG_ID, requiredString(doc, "msg_id"))
            put(AlertFields.SENT_AT, requiredLong(doc, "sent_at").toString())
            for (name in AlertFields.PAYLOAD) {
                if (payload.has(name) && !payload.isNull(name)) put(name, scalarText(payload, name))
            }
        }
        return WatchAlert.fromFields(fields)
    }

    /** 계약 watch_binding_notice. state 가 틀리거나 BOUND 인데 world_id 가 없으면 잘못된 메시지다. 모르는 method 는 null */
    private fun bindingOf(payload: JSONObject): BindingNotice {
        val state = when (requiredString(payload, "state")) {
            "BOUND" -> BindingState.BOUND
            "UNBOUND" -> BindingState.UNBOUND
            else -> throw IllegalArgumentException("state 값이 잘못됐다")
        }
        val worldId = optionalString(payload, "world_id")
        require(state == BindingState.UNBOUND || worldId != null) { "BOUND 에 world_id 가 없다" }
        return BindingNotice(
            state = state,
            method = optionalString(payload, "method")?.let(BindingMethod::parse),
            worldId = if (state == BindingState.BOUND) worldId else null,
            unbindReason = optionalString(payload, "unbind_reason"),
            sentAtMs = requiredLong(payload, "sent_at"),
        )
    }

    private fun heartbeatOf(doc: JSONObject, payload: JSONObject): Incoming.Heartbeat {
        val echo = payload.optJSONObject("echo") ?: throw IllegalArgumentException("EC2 하트비트에 echo 가 없다")
        return Incoming.Heartbeat(
            seq = requiredLong(payload, "heartbeat_seq"),
            sentAtMs = requiredLong(doc, "sent_at"),
            echoSeq = requiredLong(echo, "seq"),
            echoReceivedAtMs = requiredLong(echo, "received_at"),
            intervalMs = if (payload.has("interval_ms") && !payload.isNull("interval_ms")) requiredLong(payload, "interval_ms") else null,
        )
    }

    /** 문자열·정수·불리언 값을 그대로 글자로. 객체·배열이 온 칸은 잘못된 값이다 */
    private fun scalarText(obj: JSONObject, name: String): String {
        val value = obj.get(name)
        require(value is String || value is Number || value is Boolean) { "$name 값이 잘못됐다" }
        return value.toString()
    }

    /** 없거나 null 이면 null. 있는데 문자열이 아니거나 비어 있으면 잘못된 값이다 */
    private fun optionalString(obj: JSONObject, name: String): String? {
        if (!obj.has(name) || obj.isNull(name)) return null
        val value = obj.opt(name)
        require(value is String && value.isNotEmpty()) { "$name 값이 잘못됐다" }
        return value
    }

    private fun requiredString(obj: JSONObject, name: String): String {
        val value = obj.opt(name)
        require(value is String && value.isNotEmpty()) { "$name 가 없다" }
        return value
    }

    private fun requiredLong(obj: JSONObject, name: String): Long {
        val value = obj.opt(name)
        require(value is Int || value is Long) { "$name 는 정수여야 한다" }
        return (value as Number).toLong()
    }
}
