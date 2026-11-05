package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import com.ssafy.simai.domain.tier0.event.RiskEventsDelivered;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.lang.reflect.Type;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.messaging.simp.stomp.StompFrameHandler;
import org.springframework.messaging.simp.stomp.StompHeaders;
import org.springframework.messaging.simp.stomp.StompSession;
import org.springframework.messaging.simp.stomp.StompSessionHandlerAdapter;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import org.springframework.web.socket.messaging.WebSocketStompClient;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * S15P31A101-338 워치 진동 알림·ACK·격상 통합 시험(시험용 워치 WebSocket 클라이언트 + STOMP 관제 화면). 시험 설정은 확인 기한 1초,
 * 격상 판정은 시험이 직접 부른다(주기 판정 꺼 둠), 하트비트 1초(시험용 워치는 0.5초마다 보낸다).
 * <ul>
 * <li>임시 수동 배정 API: 관리자만, 1:1(옛 배정 REPLACED), 풀기, 없는 워치·빈 World ID·없는 배정</li>
 * <li>BOUND 워치에 계약 watch_alert 를 보내고 VIBRATED·CONFIRMED ACK 를 기록하면 격상하지 않는다(RiskEventsDelivered 경로)</li>
 * <li>ACK 가 없으면 기한 뒤 한 번만 격상하고 /topic/watch 로 watch_escalation 을 보낸다</li>
 * <li>UNBOUND·LOST·해소된 이벤트·REPLAY·INFO 이벤트는 보내지 않는다. 같은 이벤트는 한 번, 등급이 오를 때만 다시</li>
 * <li>다른 워치의 ACK 로는 알림을 고치지 못한다</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class WatchAlertIntegrationTest {

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    MachineTokenService machineTokens;

    @Autowired
    WatchBindings bindings;

    @Autowired
    WatchAlertService alerts;

    @Autowired
    ApplicationEventPublisher events;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ObjectMapper mapper;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final List<AutoCloseable> closeables = new ArrayList<>();
    private final List<UUID> eventIds = new ArrayList<>();
    private String watchId;
    private String watchToken;
    private String otherId;
    private String otherToken;
    private String worldId;
    private long adminId;

    @BeforeEach
    void setUp() {
        watchId = "watch-alert-" + tag;
        watchToken = machineTokens.registerDevice(watchId, "WATCH").token();
        otherId = "watch-other-" + tag;
        otherToken = machineTokens.registerDevice(otherId, "WATCH").token();
        worldId = "w-" + tag;
        Long id = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "watch-admin-" + tag);
        adminId = id == null ? -1 : id;
    }

    @AfterEach
    void tearDown() throws Exception {
        for (AutoCloseable c : closeables) {
            try {
                c.close();
            } catch (Exception e) {
                // 이미 닫힘
            }
        }
        waitUntil(() -> count("SELECT count(*) FROM watch_connection WHERE device_id IN (?, ?) AND ended_at IS NULL",
                watchId, otherId) == 0, 3_000);
        jdbc.update("DELETE FROM watch_alert WHERE device_id IN (?, ?)", watchId, otherId);
        jdbc.update("DELETE FROM watch_binding WHERE device_id IN (?, ?)", watchId, otherId);
        jdbc.update("DELETE FROM watch_connection WHERE device_id IN (?, ?)", watchId, otherId);
        for (UUID e : eventIds) {
            jdbc.update("DELETE FROM risk_event WHERE event_id = ?", e);
        }
        jdbc.update("DELETE FROM device WHERE device_id IN (?, ?)", watchId, otherId);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    // ------------------------------------------------------------------ 배정 API

    @Test
    void 배정_API_는_관리자만_쓰고_1대1로_잇는다() throws Exception {
        assertThat(put(watchId, worldId, jwt("VIEWER")).statusCode()).isEqualTo(403);
        // 조회는 열람 이상(-342 관제 화면 이름표)
        assertThat(send(HttpRequest.newBuilder(api("")).header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("VIEWER")).GET())
                .statusCode()).isEqualTo(200);
        assertThat(send(HttpRequest.newBuilder(api("")).GET()).statusCode()).isEqualTo(401);
        assertThat(put(watchId, worldId, null).statusCode()).isEqualTo(401);

        HttpResponse<String> bound = put(watchId, worldId, jwt("ADMIN"));
        assertThat(bound.statusCode()).isEqualTo(200);
        JsonNode body = mapper.readTree(bound.body());
        assertThat(body.path("device_id").asString()).isEqualTo(watchId);
        assertThat(body.path("world_id").asString()).isEqualTo(worldId);
        assertThat(body.path("method").asString()).isEqualTo("MANUAL");
        assertThat(body.path("channel_state").asString()).as("워치가 안 붙어 있으면 LOST").isEqualTo("LOST");

        // 같은 World ID 를 다른 워치에 이으면 옛 배정은 REPLACED
        assertThat(put(otherId, worldId, jwt("ADMIN")).statusCode()).isEqualTo(200);
        assertThat(jdbc.queryForObject("SELECT unbind_reason FROM watch_binding WHERE device_id = ?", String.class, watchId))
                .isEqualTo("REPLACED");
        HttpResponse<String> list = send(HttpRequest.newBuilder(api("")).header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("ADMIN")).GET());
        assertThat(list.statusCode()).isEqualTo(200);
        List<String> listed = new ArrayList<>();
        mapper.readTree(list.body()).forEach(b -> listed.add(b.path("device_id").asString() + "=" + b.path("world_id").asString()));
        assertThat(listed).contains(otherId + "=" + worldId).noneMatch(s -> s.startsWith(watchId + "="));

        assertThat(delete(otherId).statusCode()).isEqualTo(204);
        assertThat(delete(otherId).statusCode()).as("없는 배정").isEqualTo(404);
        assertThat(put("watch-nope-" + tag, worldId, jwt("ADMIN")).statusCode()).as("없는 워치").isEqualTo(404);
        assertThat(put(watchId, " ", jwt("ADMIN")).statusCode()).as("빈 World ID").isEqualTo(400);
    }

    // ------------------------------------------------------------------ 보내기·ACK

    @Test
    void BOUND_워치에_보내고_ACK_를_받으면_격상하지_않는다() throws Exception {
        bindings.bindManually(watchId, worldId, adminId);
        Watch watch = watch(watchId, watchToken);
        UUID event = liveEvent("R5", "HIGH", "RAISED", "LIVE", worldId);

        long before = System.currentTimeMillis();
        events.publishEvent(new RiskEventsDelivered(List.of(event))); // 실제 경로: 커밋 뒤 이벤트 → watch-alert 스레드
        JsonNode alert = watch.next("watch_alert");
        assertThat(schemas.violations("messages/watch_alert", alert.toString())).isEmpty();
        assertThat(alert.path("payload").path("event_id").asString()).isEqualTo(event.toString());
        assertThat(alert.path("payload").path("severity").asString()).isEqualTo("HIGH");
        assertThat(alert.path("payload").path("vibration_pattern").asString()).isEqualTo("CAUTION_DOUBLE");
        assertThat(alert.path("payload").path("word").asString()).isEqualTo("진입주의");
        assertThat(alert.path("payload").path("ack_within_ms").asInt()).isEqualTo(1_000);

        String alertMsgId = alert.path("msg_id").asString();
        watch.send(ack(watchId, event, alertMsgId, "VIBRATED"));
        waitUntil(() -> count("SELECT count(*) FROM watch_alert WHERE alert_msg_id = ?::uuid AND ack_received_at IS NOT NULL",
                alertMsgId) == 1, 3_000);
        watch.send(ack(watchId, event, alertMsgId, "CONFIRMED"));
        waitUntil(() -> count("SELECT count(*) FROM watch_alert WHERE alert_msg_id = ?::uuid AND confirmed_at IS NOT NULL",
                alertMsgId) == 1, 3_000);

        Thread.sleep(1_300);
        alerts.escalateOverdue();
        Map<String, Object> row = jdbc.queryForMap("SELECT * FROM watch_alert WHERE alert_msg_id = ?::uuid", alertMsgId);
        assertThat(row.get("escalated_at")).as("기한 안에 ACK 가 왔다").isNull();
        assertThat(row.get("world_id")).isEqualTo(worldId);
        assertThat(((Timestamp) row.get("sent_at")).getTime()).isGreaterThanOrEqualTo(before);
        assertThat(row.get("acked_at")).isNotNull();
        assertThat(row.get("confirmed_at")).isNotNull();
    }

    @Test
    void ACK_가_없으면_기한_뒤_한_번만_격상하고_관리자_화면에_알린다() throws Exception {
        bindings.bindManually(watchId, worldId, adminId);
        Watch watch = watch(watchId, watchToken);
        BlockingQueue<JsonNode> screen = subscribe(RealtimeTopics.WATCH);
        UUID event = liveEvent("R1", "CAUTION", "RAISED", "LIVE", worldId);

        alerts.alert(event);
        JsonNode alert = watch.next("watch_alert");
        assertThat(alert.path("payload").path("vibration_pattern").asString()).isEqualTo("CAUTION_WEAK");

        alerts.escalateOverdue();
        assertThat(screen.poll(300, TimeUnit.MILLISECONDS)).as("기한 전에는 격상하지 않는다").isNull();
        Thread.sleep(1_000);
        alerts.escalateOverdue();
        JsonNode escalation = screen.poll(5, TimeUnit.SECONDS);
        assertThat(escalation).as("확인 기한 1초가 지나면 관리자 화면에 온다").isNotNull();
        assertThat(schemas.violations("messages/watch_escalation", escalation.toString())).isEmpty();
        JsonNode p = escalation.path("payload");
        assertThat(p.path("reason").asString()).isEqualTo("NO_ACK");
        assertThat(p.path("alert_msg_id").asString()).isEqualTo(alert.path("msg_id").asString());
        assertThat(p.path("device_id").asString()).isEqualTo(watchId);
        assertThat(p.path("world_id").asString()).isEqualTo(worldId);
        assertThat(p.path("escalated_at").asLong() - p.path("sent_at").asLong()).isGreaterThanOrEqualTo(1_000);

        alerts.escalateOverdue();
        assertThat(screen.poll(600, TimeUnit.MILLISECONDS)).as("한 알림에 한 번").isNull();
        // 늦은 ACK 는 기록만 하고 격상은 그대로
        watch.send(ack(watchId, event, alert.path("msg_id").asString(), "VIBRATED"));
        waitUntil(() -> count("SELECT count(*) FROM watch_alert WHERE event_id = ? AND ack_received_at IS NOT NULL", event) == 1, 3_000);
        assertThat(jdbc.queryForObject("SELECT escalation_reason FROM watch_alert WHERE event_id = ?", String.class, event))
                .isEqualTo("NO_ACK");
    }

    @Test
    void UNBOUND_LOST_해소_REPLAY_INFO_이벤트는_보내지_않는다() throws Exception {
        Watch watch = watch(watchId, watchToken);
        alerts.alert(liveEvent("R1", "CRITICAL", "RAISED", "LIVE", worldId));
        assertThat(watch.poll(500)).as("배정 없음(UNBOUND)").isNull();

        bindings.bindManually(watchId, worldId, adminId);
        alerts.alert(liveEvent("R1", "CRITICAL", "RESOLVED", "LIVE", worldId));
        alerts.alert(liveEvent("R1", "CRITICAL", "RAISED", "REPLAY", worldId));
        alerts.alert(liveEvent("R1", "INFO", "RAISED", "LIVE", worldId));
        assertThat(watch.poll(500)).as("해소·REPLAY·INFO").isNull();

        watch.close();
        waitUntil(() -> count("SELECT count(*) FROM watch_connection WHERE device_id = ? AND ended_at IS NULL", watchId) == 0, 3_000);
        alerts.alert(liveEvent("R1", "CRITICAL", "RAISED", "LIVE", worldId));
        assertThat(count("SELECT count(*) FROM watch_alert WHERE device_id = ?", watchId)).as("LOST·위 경우 모두 기록도 없다").isZero();
    }

    @Test
    void 같은_이벤트는_한_번_보내고_등급이_오를_때만_다시_보낸다() throws Exception {
        bindings.bindManually(watchId, worldId, adminId);
        Watch watch = watch(watchId, watchToken);
        UUID event = liveEvent("R2", "HIGH", "RAISED", "LIVE", worldId);

        alerts.alert(event);
        assertThat(watch.next("watch_alert").path("payload").path("severity").asString()).isEqualTo("HIGH");
        alerts.alert(event);
        assertThat(watch.poll(400)).as("재발행").isNull();

        jdbc.update("UPDATE risk_event SET severity = 'CRITICAL', lifecycle = 'UPDATED' WHERE event_id = ?", event);
        alerts.alert(event);
        assertThat(watch.next("watch_alert").path("payload").path("severity").asString()).isEqualTo("CRITICAL");

        jdbc.update("UPDATE risk_event SET severity = 'CAUTION' WHERE event_id = ?", event);
        alerts.alert(event);
        assertThat(watch.poll(400)).as("등급이 내려가면 보내지 않는다").isNull();
        assertThat(count("SELECT count(*) FROM watch_alert WHERE event_id = ?", event)).isEqualTo(2);
    }

    @Test
    void 다른_워치의_ACK_로는_알림을_고치지_못한다() throws Exception {
        bindings.bindManually(watchId, worldId, adminId);
        Watch watch = watch(watchId, watchToken);
        Watch other = watch(otherId, otherToken);
        UUID event = liveEvent("R4", "CRITICAL", "RAISED", "LIVE", worldId);

        alerts.alert(event);
        String alertMsgId = watch.next("watch_alert").path("msg_id").asString();
        other.send(ack(otherId, event, alertMsgId, "VIBRATED"));
        Thread.sleep(500);
        assertThat(count("SELECT count(*) FROM watch_alert WHERE alert_msg_id = ?::uuid AND ack_received_at IS NOT NULL", alertMsgId))
                .isZero();
    }

    @Test
    void 해소_뒤_다시_활성이면_같은_등급이라도_다시_알린다() throws Exception {
        bindings.bindManually(watchId, worldId, adminId);
        Watch watch = watch(watchId, watchToken);
        UUID event = liveEvent("R1", "HIGH", "RAISED", "LIVE", worldId);
        alerts.alert(event);
        watch.next("watch_alert");

        jdbc.update("UPDATE risk_event SET resume_count = 1, event_sequence = 3 WHERE event_id = ?", event);
        alerts.alert(event);
        assertThat(watch.next("watch_alert").path("payload").path("event_sequence").asInt()).isEqualTo(3);
        assertThat(count("SELECT count(*) FROM watch_alert WHERE event_id = ?", event)).isEqualTo(2);
    }

    @Test
    void CONFIRMED_만_오거나_이벤트가_해소되면_격상하지_않는다() throws Exception {
        bindings.bindManually(watchId, worldId, adminId);
        Watch watch = watch(watchId, watchToken);
        UUID confirmed = liveEvent("R1", "HIGH", "RAISED", "LIVE", worldId);
        alerts.alert(confirmed);
        String msgId = watch.next("watch_alert").path("msg_id").asString();
        watch.send(ack(watchId, confirmed, msgId, "CONFIRMED")); // VIBRATED 를 잃었다
        waitUntil(() -> count("SELECT count(*) FROM watch_alert WHERE event_id = ? AND confirm_received_at IS NOT NULL",
                confirmed) == 1, 3_000);

        UUID resolved = liveEvent("R2", "HIGH", "RAISED", "LIVE", worldId);
        alerts.alert(resolved);
        watch.next("watch_alert");
        jdbc.update("UPDATE risk_event SET lifecycle = 'RESOLVED' WHERE event_id = ?", resolved);

        Thread.sleep(1_200);
        alerts.escalateOverdue();
        assertThat(count("SELECT count(*) FROM watch_alert WHERE event_id IN (?, ?) AND escalated_at IS NOT NULL", confirmed, resolved))
                .isZero();
    }

    // ------------------------------------------------------------------ 수동 알림 API (S15P31A101-475)

    @Test
    void 수동_알림은_관리자_반장이_보내고_워치가_받아_ACK_가_조회에_보인다() throws Exception {
        Watch watch = watch(watchId, watchToken);
        Map<String, Object> body = Map.of("device_id", watchId, "severity", "CRITICAL", "word", " 안전모 ", "direction", "LEFT");
        assertThat(postAlert(body, jwt("VIEWER")).statusCode()).isEqualTo(403);
        assertThat(postAlert(body, null).statusCode()).isEqualTo(401);

        HttpResponse<String> sent = postAlert(body, jwt("FOREMAN"));
        assertThat(sent.statusCode()).as(sent.body()).isEqualTo(200);
        JsonNode row = mapper.readTree(sent.body());
        assertThat(row.path("origin").asString()).isEqualTo("MANUAL");
        assertThat(row.path("word").asString()).as("앞뒤 공백은 지운다").isEqualTo("안전모");
        assertThat(row.path("world_id").isNull()).as("배정이 없어도 보낸다").isTrue();
        assertThat(row.path("requested_by_user_id").asLong()).isEqualTo(adminId);
        assertThat(row.path("ack_within_ms").asInt()).isEqualTo(1_000);

        JsonNode alert = watch.next("watch_alert");
        assertThat(schemas.violations("messages/watch_alert", alert.toString())).isEmpty();
        assertThat(alert.path("msg_id").asString()).isEqualTo(row.path("alert_msg_id").asString());
        assertThat(alert.path("payload").path("event_id").asString()).isEqualTo(row.path("event_id").asString());
        assertThat(alert.path("payload").path("event_sequence").asInt()).isEqualTo(1);
        assertThat(alert.path("payload").path("vibration_pattern").asString()).isEqualTo("DANGER_LONG");
        assertThat(alert.path("payload").path("direction").asString()).isEqualTo("LEFT");
        assertThat(alert.path("payload").path("word").asString()).isEqualTo("안전모");

        UUID event = UUID.fromString(row.path("event_id").asString());
        watch.send(ack(watchId, event, alert.path("msg_id").asString(), "VIBRATED"));
        watch.send(ack(watchId, event, alert.path("msg_id").asString(), "CONFIRMED"));
        waitUntil(() -> count("SELECT count(*) FROM watch_alert WHERE alert_id = ? AND confirm_received_at IS NOT NULL",
                row.path("alert_id").asLong()) == 1, 3_000);

        HttpResponse<String> listed = getAlerts("?device_id=" + watchId + "&limit=5", jwt("VIEWER"));
        assertThat(listed.statusCode()).isEqualTo(200);
        JsonNode first = mapper.readTree(listed.body()).get(0);
        assertThat(first.path("alert_id").asLong()).isEqualTo(row.path("alert_id").asLong());
        assertThat(first.path("acked_at").isNumber()).isTrue();
        assertThat(first.path("ack_received_at").isNumber()).isTrue();
        assertThat(first.path("confirmed_at").isNumber()).isTrue();
        assertThat(first.path("escalated_at").isNull()).isTrue();

        Thread.sleep(1_300);
        alerts.escalateOverdue();
        assertThat(jdbc.queryForObject("SELECT escalated_at FROM watch_alert WHERE alert_id = ?", Timestamp.class,
                row.path("alert_id").asLong())).as("ACK 를 받았으면 격상하지 않는다").isNull();
    }

    @Test
    void 수동_알림은_연결_없는_워치와_틀린_값을_거부한다() throws Exception {
        String token = jwt("ADMIN");
        Map<String, Object> ok = Map.of("device_id", watchId, "severity", "HIGH", "word", "호출");
        HttpResponse<String> notConnected = postAlert(ok, token);
        assertThat(notConnected.statusCode()).as("붙어 있지 않은 워치").isEqualTo(409);
        assertThat(notConnected.body()).contains("WATCH_NOT_CONNECTED");
        assertThat(count("SELECT count(*) FROM watch_alert WHERE device_id = ?", watchId)).as("행을 남기지 않는다").isZero();
        assertThat(postAlert(Map.of("device_id", "watch-nope-" + tag, "severity", "HIGH", "word", "호출"), token).statusCode()).isEqualTo(404);
        assertThat(postAlert(Map.of("device_id", watchId, "severity", "INFO", "word", "호출"), token).statusCode()).isEqualTo(400);
        assertThat(postAlert(Map.of("device_id", watchId, "severity", "HIGH", "word", "아홉글자가넘는단어"), token).statusCode()).isEqualTo(400);
        assertThat(postAlert(Map.of("device_id", watchId, "severity", "HIGH", "word", "호\n출"), token).statusCode()).as("가운데 줄바꿈")
                .isEqualTo(400);
        assertThat(postAlert(Map.of("device_id", watchId, "severity", "HIGH", "word", "호출", "direction", "UP"), token).statusCode())
                .isEqualTo(400);
        assertThat(postAlert(Map.of("device_id", watchId, "severity", "HIGH", "word", "호출", "ack_within_ms", 999), token).statusCode())
                .as("1초 미만").isEqualTo(400);
    }

    @Test
    void 수동_알림도_ACK_가_없으면_기한_뒤_격상하고_배정된_World_ID_를_싣는다() throws Exception {
        bindings.bindManually(watchId, worldId, adminId);
        Watch watch = watch(watchId, watchToken);
        BlockingQueue<JsonNode> screen = subscribe(RealtimeTopics.WATCH);

        HttpResponse<String> sent = postAlert(Map.of("device_id", watchId, "severity", "CAUTION", "word", "호출"), jwt("ADMIN"));
        assertThat(sent.statusCode()).as(sent.body()).isEqualTo(200);
        JsonNode row = mapper.readTree(sent.body());
        assertThat(row.path("world_id").asString()).isEqualTo(worldId);
        JsonNode alert = watch.next("watch_alert");
        assertThat(alert.path("payload").has("direction")).as("방향을 안 주면 싣지 않는다").isFalse();

        Thread.sleep(1_300);
        alerts.escalateOverdue();
        JsonNode escalation = screen.poll(5, TimeUnit.SECONDS);
        assertThat(escalation).isNotNull();
        assertThat(schemas.violations("messages/watch_escalation", escalation.toString())).isEmpty();
        JsonNode p = escalation.path("payload");
        assertThat(p.path("origin").asString()).isEqualTo("MANUAL");
        assertThat(p.path("reason").asString()).isEqualTo("NO_ACK");
        assertThat(p.path("alert_msg_id").asString()).isEqualTo(row.path("alert_msg_id").asString());
        assertThat(p.path("world_id").asString()).isEqualTo(worldId);
        assertThat(p.path("event_sequence").asInt()).isEqualTo(1);

        JsonNode listed = mapper.readTree(getAlerts("?device_id=" + watchId, jwt("FOREMAN")).body()).get(0);
        assertThat(listed.path("escalation_reason").asString()).isEqualTo("NO_ACK");
        assertThat(listed.path("escalated_at").isNumber()).isTrue();

        // 배정 없는 워치의 수동 알림 격상: world_id null 이 계약 1.1.0 을 통과한다. 기한 10분(상한)도 격상 창 안이다
        Watch other = watch(otherId, otherToken);
        HttpResponse<String> unbound = postAlert(Map.of("device_id", otherId, "severity", "HIGH", "word", "호출", "ack_within_ms", 600_000),
                jwt("ADMIN"));
        assertThat(unbound.statusCode()).as(unbound.body()).isEqualTo(200);
        other.next("watch_alert");
        jdbc.update("UPDATE watch_alert SET sent_at = sent_at - interval '601 seconds' WHERE alert_id = ?",
                mapper.readTree(unbound.body()).path("alert_id").asLong());
        alerts.escalateOverdue();
        JsonNode unboundEscalation = screen.poll(5, TimeUnit.SECONDS);
        assertThat(unboundEscalation).isNotNull();
        assertThat(schemas.violations("messages/watch_escalation", unboundEscalation.toString())).isEmpty();
        assertThat(unboundEscalation.path("payload").path("device_id").asString()).isEqualTo(otherId);
        assertThat(unboundEscalation.path("payload").path("world_id").isNull()).isTrue();
        JsonNode all = mapper.readTree(getAlerts("", jwt("VIEWER")).body());
        assertThat(all.isArray() && all.size() >= 2).as("기기 없이 조회하면 전체").isTrue();
    }

    private HttpResponse<String> postAlert(Map<String, Object> body, String token) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://localhost:" + port + WatchAlertController.API))
                .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)));
        if (token != null) {
            b.header(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        return send(b);
    }

    private HttpResponse<String> getAlerts(String query, String token) throws Exception {
        return send(HttpRequest.newBuilder(URI.create("http://localhost:" + port + WatchAlertController.API + query))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token).GET());
    }

    // ------------------------------------------------------------------ 도우미

    private UUID liveEvent(String ruleId, String severity, String lifecycle, String source, String world) {
        UUID event = uuid7();
        eventIds.add(event);
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity, confidence,
                    needs_check, world_ids, metrics, resume_count, event_sequence, raised_at, retention_hold)
                VALUES (?, ?, 'tier0-watch', ?, 1, ?, 'VERIFIED', ?, 'HIGH', false, ARRAY[?]::text[], '{}'::jsonb, 0, 1, now(), false)
                """, event, source, ruleId, lifecycle, severity, world);
        return event;
    }

    /** 계약 event_id 는 UUIDv7 */
    private static UUID uuid7() {
        UUID random = UUID.randomUUID();
        long high = (System.currentTimeMillis() << 16) | 0x7000L | (random.getMostSignificantBits() & 0x0FFFL);
        long low = (random.getLeastSignificantBits() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
        return new UUID(high, low);
    }

    private ObjectNode ack(String sourceId, UUID event, String alertMsgId, String kind) {
        ObjectNode root = envelope("watch_ack", sourceId);
        ObjectNode p = root.putObject("payload");
        p.put("event_id", event.toString());
        p.put("alert_msg_id", alertMsgId);
        p.put("kind", kind);
        p.put("acked_at", System.currentTimeMillis());
        return root;
    }

    private ObjectNode heartbeat(String sourceId) {
        ObjectNode root = envelope("watch_heartbeat", sourceId);
        ObjectNode p = root.putObject("payload");
        p.put("heartbeat_seq", 0);
        ObjectNode w = p.putObject("watch");
        w.put("battery_pct", 80);
        w.put("transport", "WIFI");
        return root;
    }

    private ObjectNode envelope(String msgType, String sourceId) {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", msgType);
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", System.currentTimeMillis());
        root.put("source_id", sourceId);
        return root;
    }

    /** 붙고 첫 하트비트 답을 받은 워치. 시험 동안 0.5초마다 하트비트를 보내 끊김(시험 3초)에 걸리지 않는다 */
    private Watch watch(String deviceId, String token) throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        Watch watch = new Watch(mapper);
        watch.session = new StandardWebSocketClient().execute(watch.handler, headers,
                URI.create("ws://localhost:" + port + WatchPaths.WS)).get(5, TimeUnit.SECONDS);
        closeables.add(watch);
        watch.send(heartbeat(deviceId));
        watch.next("watch_heartbeat");
        Thread.ofVirtual().start(() -> {
            while (watch.session.isOpen()) {
                try {
                    Thread.sleep(500);
                    watch.send(heartbeat(deviceId));
                } catch (Exception e) {
                    return;
                }
            }
        });
        return watch;
    }

    private BlockingQueue<JsonNode> subscribe(String topic) throws Exception {
        WebSocketStompClient client = new WebSocketStompClient(new StandardWebSocketClient());
        StompHeaders connect = new StompHeaders();
        connect.add(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("VIEWER"));
        StompSession session = client.connectAsync(URI.create("ws://localhost:" + port + RealtimeTopics.ENDPOINT),
                new WebSocketHttpHeaders(), connect, new StompSessionHandlerAdapter() { }).get(5, TimeUnit.SECONDS);
        BlockingQueue<JsonNode> queue = new LinkedBlockingQueue<>();
        session.subscribe(topic, new StompFrameHandler() {
            @Override
            public Type getPayloadType(StompHeaders headers) {
                return byte[].class;
            }

            @Override
            public void handleFrame(StompHeaders headers, Object payload) {
                queue.add(mapper.readTree(new String((byte[]) payload, StandardCharsets.UTF_8)));
            }
        });
        closeables.add(session::disconnect);
        Thread.sleep(300); // 구독이 브로커에 닿을 때까지
        return queue;
    }

    private HttpResponse<String> put(String deviceId, String world, String token) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(api("/" + deviceId)).header("Content-Type", "application/json")
                .PUT(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(Map.of("world_id", world))));
        if (token != null) {
            b.header(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        return send(b);
    }

    private HttpResponse<String> delete(String deviceId) throws Exception {
        return send(HttpRequest.newBuilder(api("/" + deviceId)).header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("ADMIN")).DELETE());
    }

    private URI api(String suffix) {
        return URI.create("http://localhost:" + port + WatchBindingController.API + suffix);
    }

    private static HttpResponse<String> send(HttpRequest.Builder request) throws Exception {
        return HttpClient.newHttpClient().send(request.build(), HttpResponse.BodyHandlers.ofString());
    }

    private String jwt(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(Long.toString(adminId)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private int count(String sql, Object... args) {
        Integer n = jdbc.queryForObject(sql, Integer.class, args);
        return n == null ? 0 : n;
    }

    private static void waitUntil(BooleanSupplier condition, long timeoutMs) throws InterruptedException {
        long deadline = System.nanoTime() + timeoutMs * 1_000_000L;
        while (!condition.getAsBoolean() && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
    }

    /** 시험용 워치. 하트비트 답은 건너뛰고 원하는 msg_type 을 기다린다 */
    private static final class Watch implements AutoCloseable {
        final BlockingQueue<JsonNode> received = new LinkedBlockingQueue<>();
        final ObjectMapper mapper;
        WebSocketSession session;
        final TextWebSocketHandler handler = new TextWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                received.add(mapper.readTree(message.getPayload()));
            }
        };

        Watch(ObjectMapper mapper) {
            this.mapper = mapper;
        }

        JsonNode next(String msgType) throws InterruptedException {
            long deadline = System.nanoTime() + 5_000_000_000L;
            while (System.nanoTime() < deadline) {
                JsonNode m = received.poll(100, TimeUnit.MILLISECONDS);
                if (m != null && msgType.equals(m.path("msg_type").asString())) {
                    return m;
                }
            }
            throw new AssertionError(msgType + " 가 5초 안에 와야 한다");
        }

        /** 하트비트 답·배정 알림(연결 때 붙는 동기화, -510)이 아닌 메시지 */
        JsonNode poll(long millis) throws InterruptedException {
            long deadline = System.nanoTime() + millis * 1_000_000L;
            while (System.nanoTime() < deadline) {
                JsonNode m = received.poll(50, TimeUnit.MILLISECONDS);
                if (m != null && !"watch_heartbeat".equals(m.path("msg_type").asString())
                        && !"watch_binding_notice".equals(m.path("msg_type").asString())) {
                    return m;
                }
            }
            return null;
        }

        synchronized void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(mapper.writeValueAsString(message)));
        }

        @Override
        public void close() throws Exception {
            if (session != null && session.isOpen()) {
                session.close();
            }
        }
    }
}
