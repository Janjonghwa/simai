package com.ssafy.simai.domain.video;

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
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.function.IntSupplier;
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
 * 상시 영상 조각(S15P31A101-330). Tier 0 토큰으로 업로드 주소를 받고(POST /api/uploads/sign, VIDEO_SEGMENT), 실제 I1 으로 video_segment 를
 * 보내 기록되는지, 관리자가 구간으로 조회하고 재생 주소를 받는지 본다. 저장소는 시험 설정의 서명만 하는 주소(실제 업로드 없음)다.
 * 시험마다 다른 gateway_id·카메라·시각을 써서 다른 시험의 행과 섞이지 않게 하고 끝나면 지운다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class VideoSegmentIntegrationTest {

    /** application-test.yaml 의 simai.tier0.token-sha256 원문 */
    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final String METRIC = "simai.i1.messages";
    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final String SHA = "a".repeat(64);

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
    private final String gatewayId = "tier0-video-" + tag;
    private final String camera = "cam-" + tag;
    private final long baseMs = Instant.parse("2026-03-01T00:00:00Z").toEpochMilli() + (long) (Math.random() * 1_000_000_000L);
    private long adminId;

    @BeforeEach
    void users() {
        adminId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id", Long.class, "video-admin-" + tag);
    }

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM video_access_log WHERE user_id = ?", adminId);
        jdbc.update("DELETE FROM upload_grant WHERE object_key IN (SELECT object_key FROM video_segment WHERE gateway_id = ?)", gatewayId);
        jdbc.update("DELETE FROM video_segment WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void Tier0_가_올린_조각을_기록하고_관리자가_구간으로_조회해_재생_주소를_받는다() throws Exception {
        UUID first = uuid7();
        UUID second = uuid7();
        ObjectNode a = segment(first, baseMs, baseMs + 59_960, sign(first, 1000));
        ObjectNode b = segment(second, baseMs + 60_000, baseMs + 119_960, sign(second, 2000));
        double storedBefore = counter("stored");
        try (Tier0 tier0 = connect()) {
            tier0.send(a);
            tier0.send(b);
        }
        awaitCount(() -> (int) (counter("stored") - storedBefore), 2);
        // 발급은 쓰인 것으로 표시된다(삭제 작업이 고아 객체로 지우지 않게)
        assertThat(jdbc.queryForObject("SELECT count(*) FROM upload_grant WHERE used_at IS NOT NULL AND object_key IN (?, ?)",
                Integer.class, key(a), key(b))).isEqualTo(2);

        // 구간이 걸치는 조각: 시작이 from 보다 이른 첫 조각도 끝이 from 뒤라 나온다
        JsonNode list = call("/api/videos?from=" + (baseMs + 30_000) + "&to=" + (baseMs + 70_000) + "&camera_id=" + camera,
                admin(), 200);
        assertThat(list.get("segments")).hasSize(2);
        JsonNode item = list.get("segments").get(0);
        assertThat(item.get("segment_id").asString()).isEqualTo(first.toString());
        assertThat(item.get("segment_start_capture_ms").asLong()).isEqualTo(baseMs);
        assertThat(item.get("container").asString()).isEqualTo("h264-annexb");
        assertThat(item.get("deleted_at_ms").isNull()).isTrue();
        // 목록에는 주소·객체 키를 싣지 않는다
        assertThat(item.has("url")).isFalse();
        assertThat(item.has("object_key")).isFalse();
        assertThat(call("/api/videos?from=" + (baseMs + 61_000) + "&to=" + (baseMs + 70_000) + "&camera_id=" + camera, admin(), 200)
                .get("segments")).hasSize(1);
        assertThat(call("/api/videos?from=" + (baseMs + 200_000) + "&to=" + (baseMs + 300_000) + "&camera_id=" + camera, admin(), 200)
                .get("segments")).isEmpty();

        JsonNode playback = call("/api/videos/" + first, admin(), 200);
        assertThat(playback.get("url").asString()).contains(key(a));
        assertThat(playback.get("content_type").asString()).isEqualTo("video/h264");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM video_access_log WHERE segment_id = ? AND user_id = ?", Integer.class,
                first, adminId)).isEqualTo(1);
    }

    @Test
    void 같은_조각_재전송은_한_번만_쓰고_발급과_다르거나_계약에_맞지_않으면_버린다() throws Exception {
        UUID id = uuid7();
        ObjectNode message = segment(id, baseMs, baseMs + 59_960, sign(id, 1000));
        UUID other = uuid7();
        ObjectNode wrongSize = segment(other, baseMs, baseMs + 59_960, sign(other, 1000));
        ((ObjectNode) wrongSize.get("payload")).put("size_bytes", 999);
        UUID unsigned = uuid7();
        ObjectNode noGrant = segment(unsigned, baseMs, baseMs + 59_960, "video-segments/" + unsigned + ".h264");
        ObjectNode backwards = segment(uuid7(), baseMs + 10, baseMs, "video-segments/x.h264");
        ObjectNode spoofed = segment(uuid7(), baseMs, baseMs + 10, "video-segments/y.h264");
        ((ObjectNode) spoofed.get("payload")).put("gateway_id", "tier0-other");
        // 1시간보다 긴 조각(목록 질의가 놓친다), 삭제 예정일이 자른 시각 + 14일이 아님(영구 보존·즉시 삭제), DB 가 받지 못하는 시각
        ObjectNode tooLong = segment(uuid7(), baseMs, baseMs + Duration.ofHours(2).toMillis(), "video-segments/z.h264");
        ObjectNode keptForever = segment(uuid7(), baseMs, baseMs + 10, "video-segments/w.h264");
        ((ObjectNode) keptForever.get("payload")).put("delete_after_ms", 9_000_000_000_000L);
        ObjectNode farFuture = segment(uuid7(), baseMs, baseMs + 10, "video-segments/v.h264");
        ((ObjectNode) farFuture.get("payload")).put("uploaded_at_ms", 9_000_000_000_000_000L);

        double stored = counter("stored");
        double duplicate = counter("duplicate");
        double invalid = counter("invalid");
        try (Tier0 tier0 = connect()) {
            tier0.send(message);
            ObjectNode resent = message.deepCopy();
            resent.put("msg_id", UUID.randomUUID().toString());
            tier0.send(resent);
            tier0.send(wrongSize);
            tier0.send(noGrant);
            tier0.send(backwards);
            tier0.send(spoofed);
            tier0.send(tooLong);
            tier0.send(keptForever);
            tier0.send(farFuture);
        }
        awaitCount(() -> (int) (counter("invalid") - invalid), 7);
        awaitCount(() -> (int) (counter("duplicate") - duplicate), 1);
        assertThat(counter("stored") - stored).isEqualTo(1.0);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM video_segment WHERE gateway_id = ?", Integer.class, gatewayId))
                .isEqualTo(1);
        jdbc.update("DELETE FROM upload_grant WHERE object_key = ?", key(other));
    }

    @Test
    void 관리자만_보고_시각이_맞지_않으면_400_지운_조각은_410() throws Exception {
        UUID id = uuid7();
        double stored = counter("stored");
        try (Tier0 tier0 = connect()) {
            tier0.send(segment(id, baseMs, baseMs + 59_960, sign(id, 1000)));
        }
        awaitCount(() -> (int) (counter("stored") - stored), 1);

        call("/api/videos?from=" + baseMs + "&to=" + (baseMs + 1), token("VIEWER"), 403);
        call("/api/videos?from=" + baseMs + "&to=" + (baseMs + 1), token("FOREMAN"), 403);
        call("/api/videos/" + id, token("FOREMAN"), 403);
        assertThat(call("/api/videos?to=" + baseMs, admin(), 400).get("code").asString()).isEqualTo("INVALID_VIDEO_QUERY");
        assertThat(call("/api/videos?from=" + (baseMs + 1) + "&to=" + baseMs, admin(), 400).get("code").asString())
                .isEqualTo("INVALID_VIDEO_QUERY");
        assertThat(call("/api/videos?from=" + baseMs + "&to=" + (baseMs + Duration.ofHours(25).toMillis()), admin(), 400)
                .get("code").asString()).isEqualTo("INVALID_VIDEO_QUERY");
        assertThat(call("/api/videos/" + uuid7(), admin(), 404).get("code").asString()).isEqualTo("SEGMENT_NOT_FOUND");

        jdbc.update("UPDATE video_segment SET deleted_at = now() WHERE segment_id = ?", id);
        assertThat(call("/api/videos/" + id, admin(), 410).get("code").asString()).isEqualTo("SEGMENT_DELETED");
        // 지운 조각도 기록은 목록에 보인다
        assertThat(call("/api/videos?from=" + baseMs + "&to=" + (baseMs + 1000) + "&camera_id=" + camera, admin(), 200)
                .at("/segments/0/deleted_at_ms").isNull()).isFalse();
    }

    @Test
    void 영상_조각_업로드는_Tier0_만_받고_segment_id_가_있어야_한다() throws Exception {
        HttpResponse<String> user = post("/api/uploads/sign", admin(), signBody(uuid7(), 10));
        assertThat(user.statusCode()).as(user.body()).isEqualTo(403);
        ObjectNode noId = signBody(uuid7(), 10);
        noId.remove("segment_id");
        HttpResponse<String> missing = post("/api/uploads/sign", TIER0_TOKEN, noId);
        assertThat(missing.statusCode()).as(missing.body()).isEqualTo(400);
        // 기록은 uuid_v7 만 받으므로 다른 판의 segment_id 는 서명하지 않는다(고아 객체가 남지 않게)
        HttpResponse<String> v4 = post("/api/uploads/sign", TIER0_TOKEN, signBody(UUID.randomUUID(), 10));
        assertThat(v4.statusCode()).as(v4.body()).isEqualTo(400);
        // 같은 segment_id 는 같은 키(재시도마다 새 주소)
        UUID id = uuid7();
        assertThat(sign(id, 10)).isEqualTo(sign(id, 10)).isEqualTo(key(id));
        jdbc.update("DELETE FROM upload_grant WHERE object_key = ?", key(id));
    }

    /** 업로드 주소를 받고 객체 키를 돌려준다(실제로 올리지는 않는다. 저장소가 서명에 묶은 내용만 받는 것은 -69 시험이 본다) */
    private String sign(UUID segmentId, long size) throws Exception {
        HttpResponse<String> response = post("/api/uploads/sign", TIER0_TOKEN, signBody(segmentId, size));
        assertThat(response.statusCode()).as(response.body()).isEqualTo(200);
        return MAPPER.readTree(response.body()).get("object_key").asString();
    }

    private static ObjectNode signBody(UUID segmentId, long size) {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("purpose", "VIDEO_SEGMENT");
        body.put("content_type", "video/h264");
        body.put("size_bytes", size);
        body.put("sha256", SHA);
        body.put("segment_id", segmentId.toString());
        return body;
    }

    /** 계약 예시 video_segment 를 이 Tier 0·카메라·구간·키로 바꾼다. 크기·해시는 발급과 같게 둔다(발급이 없으면 1000) */
    private ObjectNode segment(UUID segmentId, long startMs, long endMs, String objectKey) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("video_segment.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("segment_id", segmentId.toString());
        p.put("gateway_id", gatewayId);
        p.put("camera_id", camera);
        p.put("segment_start_capture_ms", startMs);
        p.put("segment_end_capture_ms", endMs);
        p.put("size_bytes", grantedSize(objectKey));
        p.put("sha256", "sha256:" + SHA);
        p.put("object_key", objectKey);
        return root;
    }

    private long grantedSize(String objectKey) {
        return jdbc.queryForList("SELECT size_bytes FROM upload_grant WHERE object_key = ?", Long.class, objectKey).stream()
                .findFirst().orElse(1000L);
    }

    private static String key(UUID segmentId) {
        return "video-segments/" + segmentId + ".h264";
    }

    private static String key(JsonNode message) {
        return message.get("payload").get("object_key").asString();
    }

    private double counter(String result) {
        Counter counter = meters.find(METRIC).tags("msg_type", "video_segment", "result", result).counter();
        return counter == null ? 0 : counter.count();
    }

    private static void awaitCount(IntSupplier count, int expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (count.getAsInt() < expected && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(count.getAsInt()).isEqualTo(expected);
    }

    private Tier0 connect() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TIER0_TOKEN);
        return new Tier0(new StandardWebSocketClient().execute(new AbstractWebSocketHandler() { }, headers,
                URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS));
    }

    private JsonNode call(String path, String token, int expectedStatus) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token).GET().build();
        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).as(path + " → " + response.body()).isEqualTo(expectedStatus);
        return response.body().isEmpty() ? MAPPER.nullNode() : MAPPER.readTree(response.body());
    }

    private HttpResponse<String> post(String path, String token, JsonNode body) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token).header(HttpHeaders.CONTENT_TYPE, "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build();
        return http.send(request, HttpResponse.BodyHandlers.ofString());
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

    /** 계약 uuid_v7 */
    private static UUID uuid7() {
        UUID random = UUID.randomUUID();
        long high = (System.currentTimeMillis() << 16) | 0x7000L | (random.getMostSignificantBits() & 0x0FFFL);
        long low = (random.getLeastSignificantBits() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
        return new UUID(high, low);
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
