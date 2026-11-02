package com.ssafy.simai.watch.i4

import com.ssafy.simai.watch.alert.AckKind
import com.ssafy.simai.watch.alert.AckRecord
import com.ssafy.simai.watch.alert.AlertDirection
import com.ssafy.simai.watch.alert.AlertSeverity
import com.ssafy.simai.watch.alert.VibrationPattern
import com.ssafy.simai.watch.gait.GaitFeatures
import com.ssafy.simai.watch.gait.MotionKind
import com.ssafy.simai.watch.gait.MotionTransition
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path

/** 계약 예시(contracts/examples)를 그대로 읽는다. Gradle 시험의 작업 폴더는 watch/app 이다 */
private val EXAMPLES: Path = Path.of("../../contracts/examples")
private val UUID_PATTERN = Regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

class I4CodecTest {
    private val codec = I4Codec("watch-01")

    private fun example(name: String): String = Files.readString(EXAMPLES.resolve(name))

    @Test
    fun parsesTheContractAlertExample() {
        val alert = (codec.parse(example("watch_alert.json")) as Incoming.Alert).alert
        assertEquals("0192a6f0-7c1e-7b3a-9f2d-000000000401", alert.alertMsgId)
        assertEquals("0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f", alert.eventId)
        assertEquals(2L, alert.eventSequence)
        assertEquals(AlertSeverity.CRITICAL, alert.severity)
        assertEquals(VibrationPattern.DANGER_LONG, alert.vibration)
        assertEquals(AlertDirection.FRONT, alert.direction)
        assertEquals("추락", alert.word)
        assertEquals(1760342401250L, alert.sentAtMs)
        assertEquals(10_000L, alert.ackWithinMs)
        assertFalse(alert.needsCheck)
    }

    @Test
    fun parsesTheCautionExampleWithoutOptionalFields() {
        val alert = (codec.parse(example("watch_alert.caution.json")) as Incoming.Alert).alert
        assertEquals(AlertSeverity.CAUTION, alert.severity)
        assertNull(alert.vibration)
        assertNull(alert.direction)
        assertNull(alert.eventSequence)
        assertTrue(alert.needsCheck)
        assertEquals(VibrationPattern.CAUTION_WEAK, alert.pattern)
    }

    @Test
    fun rejectsTheContractCounterexamples() {
        // word_padded 는 뺐다: 워치는 앞뒤 공백을 지우고 받는다(수신 측은 너그럽게)
        for (name in listOf("info_severity", "word_too_long", "ack_within_zero", "absolute_deadline", "unknown_direction")) {
            assertThrows(name, IllegalArgumentException::class.java) { codec.parse(example("invalid/watch_alert.$name.json")) }
        }
        assertThrows(IllegalArgumentException::class.java) { codec.parse(example("invalid/watch_heartbeat.ec2_without_echo.json")) }
    }

    @Test
    fun parsesTheEc2HeartbeatExample() {
        val hb = codec.parse(example("watch_heartbeat.ec2.json")) as Incoming.Heartbeat
        assertEquals(42L, hb.seq)
        assertEquals(1760342410090L, hb.sentAtMs)
        assertEquals(42L, hb.echoSeq)
        assertEquals(1760342410085L, hb.echoReceivedAtMs)
        assertEquals(10_000L, hb.intervalMs)
    }

    @Test
    fun unknownMessagesAreIgnoredAndBrokenOnesRejected() {
        assertEquals(Incoming.Unknown("risk_event"), codec.parse("""{"msg_type":"risk_event","payload":{}}"""))
        for (text in listOf("not json", "{}", """{"msg_type":"watch_alert"}""", """{"msg_type":"watch_alert","payload":[]}""")) {
            assertThrows(text, IllegalArgumentException::class.java) { codec.parse(text) }
        }
        // 정수 자리에 소수·문자열이 오면 거부한다
        assertThrows(IllegalArgumentException::class.java) {
            codec.parse(example("watch_heartbeat.ec2.json").replace("\"heartbeat_seq\": 42", "\"heartbeat_seq\": \"42\""))
        }
    }

    @Test
    fun heartbeatHasTheEnvelopeAndTheWatchBlock() {
        val status = WatchStatus(63, Transport.WIFI, "0.2.0")
        val doc = JSONObject(codec.heartbeat(seq = 3, echo = Echo(41, 1_000), status = status, nowMs = 2_000))
        assertEnvelope(doc, "watch_heartbeat", 2_000)
        val payload = doc.getJSONObject("payload")
        assertEquals(3, payload.getInt("heartbeat_seq"))
        assertEquals(41, payload.getJSONObject("echo").getInt("seq"))
        assertEquals(1_000, payload.getJSONObject("echo").getInt("received_at"))
        assertEquals(63, payload.getJSONObject("watch").getInt("battery_pct"))
        assertEquals("WIFI", payload.getJSONObject("watch").getString("transport"))
        assertEquals("0.2.0", payload.getJSONObject("watch").getString("app_version"))
        assertFalse(payload.has("interval_ms")) // EC2 → 워치에만 있는 칸

        val first = JSONObject(codec.heartbeat(0, null, status, 1)).getJSONObject("payload")
        assertFalse(first.has("echo"))
    }

    @Test
    fun ackCarriesTheRecordAndOnlyPresentOptionals() {
        val vibrated = AckRecord("m1", "e1", 2L, AckKind.VIBRATED, ackedAtMs = 5_000, receivedAtMs = 4_990)
        val doc = JSONObject(codec.ack(vibrated, nowMs = 5_001))
        assertEnvelope(doc, "watch_ack", 5_001)
        val payload = doc.getJSONObject("payload")
        assertEquals("m1", payload.getString("alert_msg_id"))
        assertEquals("e1", payload.getString("event_id"))
        assertEquals(2, payload.getInt("event_sequence"))
        assertEquals("VIBRATED", payload.getString("kind"))
        assertEquals(5_000, payload.getInt("acked_at"))
        assertEquals(4_990, payload.getInt("received_at"))

        val confirmed = JSONObject(codec.ack(AckRecord("m1", "e1", null, AckKind.CONFIRMED, 6_000, null), 6_001)).getJSONObject("payload")
        assertEquals("CONFIRMED", confirmed.getString("kind"))
        assertFalse(confirmed.has("event_sequence"))
        assertFalse(confirmed.has("received_at"))
    }

    @Test
    fun gaitFeaturesFollowTheContractShape() {
        val features = GaitFeatures(
            windowStartMs = 1_000, windowEndMs = 5_000, sampleRateHz = 50,
            stepTimesMs = listOf(1_400L, 1_950L), transitions = listOf(MotionTransition(1_000, MotionKind.START)),
            bandRateHz = 10, bandValues = List(40) { 0.123456 * it }, rms = 1.23456, peakFreqHz = 1.8, moving = true,
        )
        val doc = JSONObject(codec.gaitFeatures(features, clockOffsetMs = -37, nowMs = 5_050))
        assertEnvelope(doc, "gait_features", 5_050)
        val payload = doc.getJSONObject("payload")
        assertEquals("WATCH", payload.getString("clock_basis"))
        assertEquals(-37, payload.getInt("clock_offset_ms"))
        assertEquals(1_000, payload.getInt("window_start_ms"))
        assertEquals(5_000, payload.getInt("window_end_ms"))
        assertEquals(50, payload.getInt("sample_rate_hz"))
        assertEquals(2, payload.getJSONArray("step_times_ms").length())
        assertEquals("START", payload.getJSONArray("motion_transitions").getJSONObject(0).getString("kind"))
        val band = payload.getJSONObject("band_magnitude")
        assertEquals(10, band.getInt("rate_hz"))
        assertEquals(40, band.getJSONArray("values").length())
        assertEquals(0.123, band.getJSONArray("values").getDouble(1), 1e-9) // 소수 셋째 자리까지
        assertEquals(1.235, band.getDouble("rms"), 1e-9)
        assertEquals(1.8, band.getDouble("peak_freq_hz"), 1e-9)

        // 시계 차이 모름·뚜렷한 주파수 없음·전환 없음: null 은 싣고(필수 nullable) 전환 칸은 뺀다
        val still = JSONObject(codec.gaitFeatures(features.copy(transitions = emptyList(), peakFreqHz = null), null, 5_050)).getJSONObject("payload")
        assertTrue(still.isNull("clock_offset_ms"))
        assertTrue(still.has("clock_offset_ms"))
        assertFalse(still.has("motion_transitions"))
        assertTrue(still.getJSONObject("band_magnitude").isNull("peak_freq_hz"))
    }

    private fun assertEnvelope(doc: JSONObject, msgType: String, sentAt: Long) {
        assertEquals(I4_SCHEMA_VERSION, doc.getString("schema_version"))
        assertEquals(msgType, doc.getString("msg_type"))
        assertTrue(UUID_PATTERN.matches(doc.getString("msg_id")))
        assertEquals(sentAt, doc.getLong("sent_at"))
        assertEquals("watch-01", doc.getString("source_id"))
    }
}
