package com.ssafy.simai.domain.clip;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.SecureRandom;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
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
import org.springframework.web.socket.handler.AbstractWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 이벤트 클립 기록 수신(I1 media_clip·media_clip_retention)과 클립 목록·재생 주소·보존 표시 API(S15P31A101-261, 명세 11.5·12.6·14).
 * 실제 포트로 Tier 0(I1)처럼 보내고 관리자 REST 로 확인한다. 저장소는 서명 계산만 하므로 실제 버킷 없이 돈다(application-test.yaml).
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class MediaClipIntegrationTest {

    /** application-test.yaml 의 simai.tier0.token-sha256 원문 */
    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final String METRIC = "simai.i1.messages";
    private static final SecureRandom RANDOM = new SecureRandom();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    MeterRegistry meters;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String gatewayId = "tier0-clip-" + tag;
    private final List<UUID> events = new ArrayList<>();
    private long adminId;

    @BeforeEach
    void users() {
        adminId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id", Long.class, "clip-admin-" + tag);
    }

    @AfterEach
    void cleanUp() {
        for (UUID event : events) {
            jdbc.update("DELETE FROM clip_access_log WHERE clip_id IN (SELECT clip_id FROM media_clip WHERE event_id = ?)", event);
            jdbc.update("DELETE FROM media_clip_retention WHERE event_id = ?", event);
            jdbc.update("DELETE FROM media_clip WHERE event_id = ?", event);
            jdbc.update("DELETE FROM upload_grant WHERE event_id = ?", event);
            jdbc.update("DELETE FROM i1_deferred_message WHERE envelope -> 'payload' ->> 'event_id' = ?", event.toString());
            jdbc.update("DELETE FROM risk_event_update WHERE event_id = ?", event);
            jdbc.update("DELETE FROM risk_event WHERE event_id = ?", event);
        }
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 받은_클립은_이벤트_목록에_보이고_발급을_쓰인_것으로_표시하며_CRITICAL_은_자동_보존_이력을_남긴다() throws Exception {
        UUID event = event("CRITICAL");
        ObjectNode clip = clip(event, "cam_A", 0, "CRITICAL", true);

        send(clip);

        awaitCount(() -> clips(event), 1);
        JsonNode list = call("GET", "/api/events/" + event + "/clips", admin(), null, 200);
        assertThat(list.get("event_id").asString()).isEqualTo(event.toString());
        JsonNode item = list.get("clips").get(0);
        JsonNode p = clip.get("payload");
        assertThat(item.get("clip_id").asString()).isEqualTo(p.get("clip_id").asString());
        assertThat(item.get("camera_id").asString()).isEqualTo("cam_A");
        assertThat(item.get("resume_count").asInt()).isZero();
        assertThat(item.get("clip_start_capture_ms").asLong()).isEqualTo(p.get("clip_start_capture_ms").asLong());
        assertThat(item.get("clip_end_capture_ms").asLong()).isEqualTo(p.get("clip_end_capture_ms").asLong());
        assertThat(item.get("container").asString()).isEqualTo("h264-annexb");
        assertThat(item.get("retention_hold").asBoolean()).isTrue();
        assertThat(item.get("auto_retained").asBoolean()).isTrue();
        assertThat(item.get("deleted_at_ms").isNull()).isTrue();
        assertThat(jdbc.queryForObject("SELECT used_at IS NOT NULL FROM upload_grant WHERE object_key = ?", Boolean.class,
                p.get("object_key").asString())).isTrue();
        assertThat(jdbc.queryForObject("SELECT reason FROM media_clip_retention WHERE clip_id = ?::uuid", String.class,
                p.get("clip_id").asString())).isEqualTo("CRITICAL_AUTO");
    }

    @Test
    void 재생_주소는_관리자에게만_서명_주소로_주고_요청마다_조회_기록을_남긴다() throws Exception {
        UUID event = event("HIGH");
        ObjectNode clip = clip(event, "cam_B", 0, "HIGH", false);
        send(clip);
        awaitCount(() -> clips(event), 1);
        String clipId = clip.at("/payload/clip_id").asString();

        long before = Instant.now().toEpochMilli();
        JsonNode playback = call("GET", "/api/clips/" + clipId, admin(), null, 200);
        call("GET", "/api/clips/" + clipId, admin(), null, 200);

        assertThat(playback.get("clip_id").asString()).isEqualTo(clipId);
        assertThat(playback.get("url").asString()).startsWith("http://127.0.0.1:9000/simai-test-bucket/")
                .contains(clip.at("/payload/object_key").asString()).contains("X-Amz-Signature=");
        assertThat(playback.get("expires_at_ms").asLong()).isBetween(before + 290_000, before + 310_000);
        assertThat(playback.get("content_type").asString()).isEqualTo("video/h264");
        assertThat(playback.get("container").asString()).isEqualTo("h264-annexb");
        Map<String, Object> log = jdbc.queryForMap(
                "SELECT count(*) AS n, max(user_id) AS user_id FROM clip_access_log WHERE clip_id = ?::uuid", clipId);
        assertThat(((Number) log.get("n")).intValue()).isEqualTo(2);
        assertThat(((Number) log.get("user_id")).longValue()).isEqualTo(adminId);

        // 반장·열람은 볼 수 없고 기록도 남지 않는다
        call("GET", "/api/clips/" + clipId, token("FOREMAN"), null, 403);
        call("GET", "/api/clips/" + clipId, token("VIEWER"), null, 403);
        call("GET", "/api/events/" + event + "/clips", token("FOREMAN"), null, 403);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM clip_access_log WHERE clip_id = ?::uuid", Integer.class, clipId))
                .isEqualTo(2);
        // 없는 클립 404, 지운 클립 410(기록은 목록에 남는다), 없는 이벤트 404
        assertThat(call("GET", "/api/clips/" + uuid7(), admin(), null, 404).get("code").asString()).isEqualTo("CLIP_NOT_FOUND");
        jdbc.update("UPDATE media_clip SET deleted_at = now() WHERE clip_id = ?::uuid", clipId);
        assertThat(call("GET", "/api/clips/" + clipId, admin(), null, 410).get("code").asString()).isEqualTo("CLIP_DELETED");
        assertThat(call("GET", "/api/events/" + event + "/clips", admin(), null, 200).at("/clips/0/deleted_at_ms").isNull()).isFalse();
        assertThat(call("GET", "/api/events/" + uuid7() + "/clips", admin(), null, 404).get("code").asString())
                .isEqualTo("EVENT_NOT_FOUND");
    }

    @Test
    void 관리자는_HIGH_클립의_보존_표시를_붙이고_떼며_바뀔_때만_이력을_남기고_CRITICAL_은_떼지_못한다() throws Exception {
        UUID high = event("HIGH");
        UUID critical = event("CRITICAL");
        ObjectNode highClip = clip(high, "cam_A", 0, "HIGH", false);
        ObjectNode criticalClip = clip(critical, "cam_A", 0, "CRITICAL", true);
        send(highClip);
        send(criticalClip);
        awaitCount(() -> clips(high) + clips(critical), 2);
        String highId = highClip.at("/payload/clip_id").asString();
        String criticalId = criticalClip.at("/payload/clip_id").asString();

        JsonNode held = call("PUT", "/api/clips/" + highId + "/retention", admin(), retention(true), 200);
        call("PUT", "/api/clips/" + highId + "/retention", admin(), retention(true), 200);
        assertThat(held.get("retention_hold").asBoolean()).isTrue();
        assertThat(held.get("auto_retained").asBoolean()).isFalse();
        JsonNode released = call("PUT", "/api/clips/" + highId + "/retention", admin(), retention(false), 200);
        assertThat(released.get("retention_hold").asBoolean()).isFalse();
        assertThat(jdbc.queryForList("""
                SELECT reason FROM media_clip_retention WHERE clip_id = ?::uuid AND actor_user_id = ? ORDER BY retention_id
                """, String.class, highId, adminId)).containsExactly("ADMIN_HOLD", "ADMIN_RELEASE");

        assertThat(call("PUT", "/api/clips/" + criticalId + "/retention", admin(), retention(false), 409).get("code").asString())
                .isEqualTo("CLIP_AUTO_RETAINED");
        assertThat(call("PUT", "/api/clips/" + highId + "/retention", admin(), MAPPER.createObjectNode(), 400).get("code").asString())
                .isEqualTo("INVALID_RETENTION");
        call("PUT", "/api/clips/" + highId + "/retention", token("FOREMAN"), retention(true), 403);
        call("PUT", "/api/clips/" + uuid7() + "/retention", admin(), retention(true), 404);
        // 지운 클립에는 보존 표시를 붙이지 않는다(되살릴 수 없다)
        jdbc.update("UPDATE media_clip SET deleted_at = now() WHERE clip_id = ?::uuid", highId);
        call("PUT", "/api/clips/" + highId + "/retention", admin(), retention(true), 410);
    }

    @Test
    void 보낸_뒤_CRITICAL_로_오르면_Tier0_보존_기록이_표시를_켜고_재전송은_한_번만_남긴다() throws Exception {
        UUID event = event("HIGH");
        ObjectNode clip = clip(event, "cam_C", 0, "HIGH", false);
        send(clip);
        awaitCount(() -> clips(event), 1);
        String clipId = clip.at("/payload/clip_id").asString();
        ObjectNode retention = message("media_clip_retention.json");
        ObjectNode p = (ObjectNode) retention.get("payload");
        p.put("clip_id", clipId).put("event_id", event.toString()).put("gateway_id", gatewayId).put("resume_count", 0);
        double duplicateBefore = counter("media_clip_retention", "duplicate");

        send(retention);
        send(retention);

        awaitCount(() -> (int) (counter("media_clip_retention", "duplicate") - duplicateBefore), 1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM media_clip_retention WHERE clip_id = ?::uuid", Integer.class, clipId))
                .isOne();
        JsonNode item = call("GET", "/api/events/" + event + "/clips", admin(), null, 200).get("clips").get(0);
        assertThat(item.get("retention_hold").asBoolean()).isTrue();
        // CRITICAL 자동 보존이 붙었으니 관리자가 떼지 못한다
        assertThat(item.get("auto_retained").asBoolean()).isTrue();
        call("PUT", "/api/clips/" + clipId + "/retention", admin(), retention(false), 409);
    }

    @Test
    void 발급과_다른_기록_같은_구간의_다른_클립_다른_게이트웨이는_버리고_재전송은_중복_이벤트_전_클립은_실패로_센다() throws Exception {
        UUID event = event("HIGH");
        ObjectNode ok = clip(event, "cam_A", 0, "HIGH", false);
        ObjectNode otherHash = clip(event, "cam_B", 0, "HIGH", false);
        ((ObjectNode) otherHash.get("payload")).put("sha256", "sha256:" + "0".repeat(64));
        ObjectNode noGrant = clip(event, "cam_C", 0, "HIGH", false);
        jdbc.update("DELETE FROM upload_grant WHERE object_key = ?", noGrant.at("/payload/object_key").asString());
        ObjectNode sameSpan = clip(event, "cam_A", 0, "HIGH", false);
        ObjectNode otherGateway = clip(event, "cam_D", 0, "HIGH", false);
        ((ObjectNode) otherGateway.get("payload")).put("gateway_id", "tier0-someone-else");
        UUID missing = UUID.fromString(uuid7());
        ObjectNode beforeEvent = clip(missing, "cam_A", 0, "HIGH", false);
        // 형식과 담는 방식이 어긋나면 화면이 재생하지 못한다
        ObjectNode wrongContainer = clip(event, "cam5", 0, "HIGH", false);
        ((ObjectNode) wrongContainer.get("payload")).put("container", "mp4");
        // 다른 Tier 0 의 이벤트, REPLAY 이벤트에는 클립이 없다
        UUID foreign = event("HIGH");
        jdbc.update("UPDATE risk_event SET gateway_id = 'tier0-someone-else' WHERE event_id = ?", foreign);
        ObjectNode foreignEvent = clip(foreign, "cam_A", 0, "HIGH", false);
        UUID replay = event("HIGH");
        jdbc.update("UPDATE risk_event SET source = 'REPLAY' WHERE event_id = ?", replay);
        ObjectNode replayEvent = clip(replay, "cam_A", 0, "HIGH", false);
        // 서명은 형식을 소문자로 바꿔 발급한다. 기록이 대문자여도 같은 형식이다
        ObjectNode upperType = clip(event, "cam6", 0, "HIGH", false);
        ((ObjectNode) upperType.get("payload")).put("content_type", "Video/H264");
        double invalidBefore = counter("media_clip", "invalid");
        double duplicateBefore = counter("media_clip", "duplicate");
        double failedBefore = counter("media_clip", "failed");

        try (Tier0 tier0 = tier0()) {
            tier0.send(ok);
            tier0.send(ok);
            tier0.send(otherHash);
            tier0.send(noGrant);
            tier0.send(sameSpan);
            tier0.send(otherGateway);
            tier0.send(beforeEvent);
            tier0.send(wrongContainer);
            tier0.send(foreignEvent);
            tier0.send(replayEvent);
            tier0.send(upperType);
        }

        awaitCount(() -> (int) (counter("media_clip", "invalid") - invalidBefore), 7);
        awaitCount(() -> clips(event), 2);
        // 이벤트 전 클립은 실패가 아니라 보관했다가 다시 처리한다(클립 기록은 Tier 0 가 다시 보내지 않는다)
        assertThat(counter("media_clip", "failed") - failedBefore).isZero();
        assertThat(deferred(beforeEvent)).isOne();
        assertThat(counter("media_clip", "duplicate") - duplicateBefore).isEqualTo(1.0);
        assertThat(clips(foreign) + clips(replay)).isZero();
        // 발급을 쓴 시각은 EC2 가 받은 시각(received_at)이다
        assertThat(jdbc.queryForObject("""
                SELECT g.used_at = c.received_at FROM upload_grant g JOIN media_clip c ON c.object_key = g.object_key
                WHERE c.clip_id = ?::uuid
                """, Boolean.class, ok.at("/payload/clip_id").asString())).isTrue();
    }

    @Test
    void 이벤트보다_먼저_온_클립과_클립보다_먼저_온_보존_기록은_보관했다가_앞선_메시지가_오면_저장한다() throws Exception {
        UUID event = UUID.fromString(uuid7());
        events.add(event);
        ObjectNode clip = clip(event, "cam_A", 0, "HIGH", false);
        String clipId = clip.at("/payload/clip_id").asString();
        ObjectNode retention = message("media_clip_retention.json");
        ((ObjectNode) retention.get("payload")).put("clip_id", clipId).put("event_id", event.toString())
                .put("gateway_id", gatewayId).put("resume_count", 0);

        send(retention);
        send(clip);
        awaitCount(() -> deferred(clip) + deferred(retention), 2);
        Instant firstReceivedAt = deferredReceivedAt(clip);
        // 재전송은 보관함에서도 한 번. 이벤트를 넣기 전에 처리된 것을 확인한다(Tier 0 연결마다 처리가 비동기라 기다리지 않으면
        // 이벤트 뒤에 처리될 수 있다. 그 경우는 아래 다음 시험이 따로 본다, Jenkins #757·#771)
        double duplicateBefore = counter("media_clip", "duplicate");
        send(clip);
        awaitCount(() -> (int) (counter("media_clip", "duplicate") - duplicateBefore), 1);
        assertThat(deferred(clip)).isEqualTo(1);
        assertThat(clips(event)).isZero();
        insertEvent(event, "HIGH");

        awaitCount(() -> clips(event), 1, Duration.ofSeconds(20));
        awaitCount(() -> jdbc.queryForObject("SELECT count(*) FROM media_clip_retention WHERE clip_id = ?::uuid", Integer.class,
                clipId), 1, Duration.ofSeconds(20));
        // 재시도는 저장을 커밋한 뒤 보관 행을 따로 지운다. 저장이 보여도 삭제 전일 수 있어 기다린다
        awaitZero(() -> deferred(clip) + deferred(retention), Duration.ofSeconds(10));
        JsonNode item = call("GET", "/api/events/" + event + "/clips", admin(), null, 200).get("clips").get(0);
        assertThat(item.get("retention_hold").asBoolean()).isTrue();
        // 저장 때 받은 시각은 처음 받은 시각(보관한 시각)이다
        assertThat(clipReceivedAt(clipId)).isEqualTo(firstReceivedAt);
    }

    @Test
    void 이벤트보다_먼저_온_클립의_재전송이_이벤트_뒤에_와서_바로_저장돼도_받은_시각은_처음_받은_시각이고_보관_행은_함께_지운다()
            throws Exception {
        UUID event = UUID.fromString(uuid7());
        events.add(event);
        ObjectNode clip = clip(event, "cam_A", 0, "HIGH", false);
        String clipId = clip.at("/payload/clip_id").asString();

        // 처음 받은 클립을 보관한 상태를 직접 만든다. 다음 시도를 미뤄 재시도가 먼저 저장하지 않게 한다(재시도가 보관 행을 읽은 뒤
        // 미루기를 덮어쓰는 경쟁이 없도록 처음부터 미룬 행으로 넣는다). 이벤트 뒤에 온 재전송이 바로 저장하는 길만 남는다
        Instant firstReceivedAt = Instant.now().minusSeconds(5).truncatedTo(ChronoUnit.MICROS);
        jdbc.update("""
                INSERT INTO i1_deferred_message (msg_id, msg_type, envelope, reason, received_at, next_attempt_at)
                VALUES (?::uuid, 'media_clip', ?::jsonb, '이벤트 전이를 아직 받지 못했다', ?, now() + interval '1 hour')
                """, clip.get("msg_id").asString(), MAPPER.writeValueAsString(clip), java.sql.Timestamp.from(firstReceivedAt));
        insertEvent(event, "HIGH");
        double storedBefore = counter("media_clip", "stored");

        send(clip);

        awaitCount(() -> (int) (counter("media_clip", "stored") - storedBefore), 1);
        assertThat(clips(event)).isEqualTo(1);
        // 보관 행은 저장과 같은 트랜잭션에서 지운다(저장이 보이면 이미 없다)
        assertThat(deferred(clip)).isZero();
        assertThat(clipReceivedAt(clipId)).isEqualTo(firstReceivedAt);
        assertThat(jdbc.queryForObject("""
                SELECT c.received_at <= e.raised_at FROM media_clip c JOIN risk_event e ON e.event_id = c.event_id WHERE c.clip_id = ?::uuid
                """, Boolean.class, clipId)).isTrue();

        // 그 뒤 재전송은 중복이고 행이 늘지 않는다
        double duplicateBefore = counter("media_clip", "duplicate");
        send(clip);
        awaitCount(() -> (int) (counter("media_clip", "duplicate") - duplicateBefore), 1);
        assertThat(clips(event)).isEqualTo(1);
        assertThat(clipReceivedAt(clipId)).isEqualTo(firstReceivedAt);
    }

    @Test
    void 보존_표시가_꺼진_CRITICAL_클립은_켜서_저장하고_전이와_등급이_다르거나_지운_클립의_보존_기록은_반영하지_않는다() throws Exception {
        UUID critical = event("CRITICAL");
        ObjectNode unheld = clip(critical, "cam_A", 0, "CRITICAL", false);
        UUID high = event("HIGH");
        // 클립을 만든 전이(event_sequence 1)는 HIGH 인데 CRITICAL 이라 적은 클립
        ObjectNode wrongSeverity = clip(high, "cam_A", 0, "CRITICAL", true);
        ObjectNode highClip = clip(high, "cam_B", 0, "HIGH", false);
        double invalidBefore = counter("media_clip", "invalid");

        send(unheld);
        send(wrongSeverity);
        send(highClip);

        awaitCount(() -> clips(critical) + clips(high), 2);
        assertThat(counter("media_clip", "invalid") - invalidBefore).isEqualTo(1.0);
        JsonNode item = call("GET", "/api/events/" + critical + "/clips", admin(), null, 200).get("clips").get(0);
        assertThat(item.get("retention_hold").asBoolean()).isTrue();
        assertThat(item.get("auto_retained").asBoolean()).isTrue();

        String highId = highClip.at("/payload/clip_id").asString();
        jdbc.update("UPDATE media_clip SET deleted_at = now() WHERE clip_id = ?::uuid", highId);
        ObjectNode retention = message("media_clip_retention.json");
        ((ObjectNode) retention.get("payload")).put("clip_id", highId).put("event_id", high.toString())
                .put("gateway_id", gatewayId).put("resume_count", 0);
        double duplicateBefore = counter("media_clip_retention", "duplicate");
        send(retention);
        awaitCount(() -> (int) (counter("media_clip_retention", "duplicate") - duplicateBefore), 1);
        assertThat(jdbc.queryForObject("SELECT retention_hold FROM media_clip WHERE clip_id = ?::uuid", Boolean.class, highId)).isFalse();
    }

    @Test
    void 보존_표시가_있는_HIGH_클립은_자동_보존이_아니라_관리자가_떼고_다른_Tier0_의_보존_기록은_버린다() throws Exception {
        UUID event = event("HIGH");
        ObjectNode clip = clip(event, "cam_A", 0, "HIGH", true);
        send(clip);
        awaitCount(() -> clips(event), 1);
        String clipId = clip.at("/payload/clip_id").asString();
        // CRITICAL_AUTO 는 CRITICAL 에만 남긴다
        assertThat(jdbc.queryForObject("SELECT count(*) FROM media_clip_retention WHERE clip_id = ?::uuid", Integer.class, clipId))
                .isZero();
        ObjectNode retention = message("media_clip_retention.json");
        retention.put("source_id", "tier0-someone-else");
        ((ObjectNode) retention.get("payload")).put("clip_id", clipId).put("event_id", event.toString())
                .put("gateway_id", "tier0-someone-else").put("resume_count", 0);
        double invalidBefore = counter("media_clip_retention", "invalid");

        send(retention);

        awaitCount(() -> (int) (counter("media_clip_retention", "invalid") - invalidBefore), 1);
        JsonNode item = call("GET", "/api/events/" + event + "/clips", admin(), null, 200).get("clips").get(0);
        assertThat(item.get("retention_hold").asBoolean()).isTrue();
        assertThat(item.get("auto_retained").asBoolean()).isFalse();
        assertThat(call("PUT", "/api/clips/" + clipId + "/retention", admin(), retention(false), 200).get("retention_hold").asBoolean())
                .isFalse();
    }

    // ---- 도우미

    /** 클립이 가리킬 LIVE 이벤트와 그 발생 전이(event_sequence 1). 실제로는 risk_event_transition 으로 먼저 온다 */
    private UUID event(String severity) {
        UUID event = UUID.fromString(uuid7());
        events.add(event);
        insertEvent(event, severity);
        return event;
    }

    private void insertEvent(UUID event, String severity) {
        // raised_at 은 JVM 시계로 넣는다. 서버가 적는 received_at 도 JVM 시계라, DB now()(DB 컨테이너 시계·트랜잭션 시작 시각)를
        // 쓰면 Jenkins 에서 두 시계가 어긋날 때 먼저 받은 클립이 이벤트보다 늦게 보인다(Jenkins #556)
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity, confidence,
                    needs_check, world_ids, metrics, resume_count, raised_at, retention_hold)
                VALUES (?, 'LIVE', ?, 'R2', 1, 'RAISED', 'VERIFIED', ?, 'HIGH', false, '{}', '{}'::jsonb, 0, ?, false)
                """, event, gatewayId, severity, java.sql.Timestamp.from(java.time.Instant.now()));
        jdbc.update("""
                INSERT INTO risk_event_update (event_id, event_sequence, msg_id, gateway_id, journal_offset, transition, occurred_at,
                    observation_at, evaluated_at, rule_id, rule_version, config_version, calibration_version, scene_version, source,
                    severity, confidence, verification, needs_check, world_ids, metrics, resent, resumed, delivery_mode, received_at)
                VALUES (?, 1, ?, ?, ?, 'RAISED', now(), now(), now(), 'R2', 1, 'cfg-1', 'cal-1', 'scene-1', 'LIVE', ?, 'HIGH', 'VERIFIED',
                    false, '{}', '{}'::jsonb, false, false, 'REALTIME', now())
                """, event, UUID.randomUUID(), gatewayId, RANDOM.nextLong(1, Long.MAX_VALUE), severity);
    }

    /** 계약 예시 media_clip 을 이 이벤트·카메라로 바꾸고, EC2 가 했을 업로드 발급(upload_grant)을 같은 내용으로 남긴다 */
    private ObjectNode clip(UUID event, String camera, int resumeCount, String severity, boolean hold) throws Exception {
        ObjectNode root = message("media_clip.json");
        ObjectNode p = (ObjectNode) root.get("payload");
        String clipId = uuid7();
        byte[] digest = new byte[32];
        RANDOM.nextBytes(digest);
        String sha = HexFormat.of().formatHex(digest);
        String key = "event-clips/" + event + "/" + clipId + ".h264";
        p.put("clip_id", clipId).put("event_id", event.toString()).put("gateway_id", gatewayId).put("camera_id", camera)
                .put("resume_count", resumeCount).put("severity", severity).put("retention_hold", hold)
                .put("sha256", "sha256:" + sha).put("object_key", key);
        if (!events.contains(event)) {
            events.add(event);
        }
        jdbc.update("""
                INSERT INTO upload_grant (object_key, purpose, requester, content_type, size_bytes, sha256, event_id, issued_at, expires_at)
                VALUES (?, 'EVENT_CLIP', 'TIER0', ?, ?, ?, ?, now(), now() + interval '5 minutes')
                """, key, p.get("content_type").asString(), p.get("size_bytes").asLong(), sha, event);
        return root;
    }

    private ObjectNode message(String example) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples").resolve(example)));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", Instant.now().toEpochMilli());
        root.put("source_id", gatewayId);
        return root;
    }

    private static ObjectNode retention(boolean hold) {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("retention_hold", hold);
        return body;
    }

    /** 계약 uuid_v7: 앞 48비트 ms 시각, 버전 7, 변형 10 */
    private static String uuid7() {
        long ms = System.currentTimeMillis();
        long msb = (ms << 16) | 0x7000L | (RANDOM.nextLong() & 0x0FFFL);
        long lsb = (RANDOM.nextLong() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
        return new UUID(msb, lsb).toString();
    }

    private int clips(UUID event) {
        return jdbc.queryForObject("SELECT count(*) FROM media_clip WHERE event_id = ?", Integer.class, event);
    }

    /** 지표는 앱 전체에서 누적된다. 시험은 보내기 전후 차이만 본다 */
    private double counter(String msgType, String result) {
        Counter counter = meters.find(METRIC).tags("msg_type", msgType, "result", result).counter();
        return counter == null ? 0 : counter.count();
    }

    private int deferred(JsonNode message) {
        return jdbc.queryForObject("SELECT count(*) FROM i1_deferred_message WHERE msg_id = ?::uuid", Integer.class,
                message.get("msg_id").asString());
    }

    private Instant deferredReceivedAt(JsonNode message) {
        return jdbc.queryForObject("SELECT received_at FROM i1_deferred_message WHERE msg_id = ?::uuid", java.sql.Timestamp.class,
                message.get("msg_id").asString()).toInstant();
    }

    private Instant clipReceivedAt(String clipId) {
        return jdbc.queryForObject("SELECT received_at FROM media_clip WHERE clip_id = ?::uuid", java.sql.Timestamp.class, clipId)
                .toInstant();
    }

    private static void awaitCount(Supplier<Integer> actual, int expected) throws InterruptedException {
        awaitCount(actual, expected, Duration.ofSeconds(10));
    }

    private static void awaitZero(Supplier<Integer> actual, Duration timeout) throws InterruptedException {
        long deadline = System.nanoTime() + timeout.toNanos();
        while (actual.get() > 0 && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(actual.get()).isZero();
    }

    private static void awaitCount(Supplier<Integer> actual, int expected, Duration timeout) throws InterruptedException {
        long deadline = System.nanoTime() + timeout.toNanos();
        while (actual.get() < expected && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(actual.get()).isEqualTo(expected);
    }

    private void send(JsonNode message) throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(message);
        }
    }

    private JsonNode call(String method, String path, String token, JsonNode body, int expectedStatus) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .header(HttpHeaders.CONTENT_TYPE, "application/json")
                .method(method, body == null ? HttpRequest.BodyPublishers.noBody()
                        : HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)));
        HttpResponse<String> response = http.send(request.build(), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).as(method + " " + path + " → " + response.body()).isEqualTo(expectedStatus);
        return response.body().isEmpty() ? MAPPER.nullNode() : MAPPER.readTree(response.body());
    }

    private String admin() {
        return token("ADMIN");
    }

    private String token(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(adminId)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private Tier0 tier0() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TIER0_TOKEN);
        WebSocketSession session = new StandardWebSocketClient().execute(new AbstractWebSocketHandler() { }, headers,
                URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS);
        return new Tier0(session);
    }

    private record Tier0(WebSocketSession session) implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(MAPPER.writeValueAsString(message)));
        }

        @Override
        public void close() throws Exception {
            session.close();
        }
    }
}
