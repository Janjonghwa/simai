package com.ssafy.simai.domain.calibration;

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
 * Tier 0 보정 상태 알림(I1 calibration_update)과 보정 상태·설치 안내 조회(S15P31A101-263, 명세 4.4·4.5, FR-25·26).
 * 실제 포트로 Tier 0(I1)처럼 보내고 GET /api/calibrations/current 가 가장 늦게 알린 상태를 주는지 본다.
 * 현재 보정은 표 전체에서 sent_at 이 가장 늦은 알림이라, 시험은 지금보다 뒤 시각으로 보내 다른 행보다 늦게 만든다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class CalibrationStatusIntegrationTest {

    /** application-test.yaml 의 simai.tier0.token-sha256 원문 */
    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final String METRIC = "simai.i1.messages";
    /** 계약 예시 calibration.json 의 content_hash(Python 으로 잰 값, CalibrationContentHashTest). 같은 내용의 문서를 올리면 이 해시가 된다 */
    private static final String EXAMPLE_HASH = "sha256:75ffde54159aff5cf3cc892429f920c2b3d12ce45c3b35af89eb6f54393ae846";

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
    private final String gatewayId = "tier0-cal-" + tag;
    private final String prefix = "cal-test263-" + tag;
    /** 다른 시험·실행이 남긴 알림보다 늦게 */
    private final long base = Instant.now().plus(Duration.ofDays(1)).toEpochMilli();
    private long adminId;

    @BeforeEach
    void users() {
        adminId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id", Long.class, "cal-admin-" + tag);
    }

    @AfterEach
    void cleanUp() {
        String like = prefix + "%";
        jdbc.update("DELETE FROM calibration_update_message WHERE calibration_version LIKE ?", like);
        jdbc.update("DELETE FROM coverage_guide WHERE calibration_version LIKE ?", like);
        jdbc.update("DELETE FROM calibration_document WHERE calibration_version LIKE ?", like);
        // 이전 버전을 가리키는 행부터 지운다
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version LIKE ? AND previous_calibration_version IS NOT NULL", like);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version LIKE ?", like);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 알림을_받으면_현재_보정이_그_상태와_버전과_카메라별_오차를_돌려준다() throws Exception {
        ObjectNode first = update(prefix + "-a", null, "PROVISIONAL", "INITIAL", base);

        try (Tier0 tier0 = tier0()) {
            tier0.send(first);
        }

        awaitCount(() -> messages(prefix + "-a"), 1);
        JsonNode current = call("/api/calibrations/current", viewer(), 200);
        assertThat(current.get("calibration_state").asString()).isEqualTo("PROVISIONAL");
        assertThat(current.get("calibration_version").asString()).isEqualTo(prefix + "-a");
        assertThat(current.get("provider_source").asString()).isEqualTo("G1_AUTO");
        assertThat(current.get("reason").asString()).isEqualTo("INITIAL");
        assertThat(current.get("updated_at").asLong()).isEqualTo(base);
        assertThat(current.get("effective_at").asLong()).isEqualTo(base + 600);
        assertThat(current.get("cameras").get(0).get("camera_id").asString()).isEqualTo("cam2");
        assertThat(current.get("cameras").get(0).at("/reprojection_error_px/p95").asDouble()).isEqualTo(8.9);
        assertThat(current.get("document_uploaded").asBoolean()).isFalse();
        assertThat(jdbc.queryForObject("SELECT state FROM calibration_version WHERE calibration_version = ?", String.class,
                prefix + "-a")).isEqualTo("PROVISIONAL");
    }

    @Test
    void DRIFTED_는_같은_버전의_현재_상태만_바꾸고_늦게_온_옛_알림은_현재가_되지_않는다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "CALIBRATED", "INITIAL", base));
            awaitCount(() -> messages(prefix + "-a"), 1);
            tier0.send(update(prefix + "-a", null, "DRIFTED", "DRIFT_DETECTED", base + 2_000));
            awaitCount(() -> messages(prefix + "-a"), 2);
            // 재전송이 늦게 도착: Tier 0 시계로 더 이른 알림
            tier0.send(update(prefix + "-a", null, "PROVISIONAL", "STATE_CHANGED", base + 1_000));
        }

        awaitCount(() -> messages(prefix + "-a"), 3);
        JsonNode current = call("/api/calibrations/current", viewer(), 200);
        assertThat(current.get("calibration_state").asString()).isEqualTo("DRIFTED");
        assertThat(current.get("reason").asString()).isEqualTo("DRIFT_DETECTED");
        assertThat(current.get("updated_at").asLong()).isEqualTo(base + 2_000);
        // 버전 행은 덮어쓰지 않는다(처음 알린 값 그대로)
        assertThat(jdbc.queryForObject("SELECT state FROM calibration_version WHERE calibration_version = ?", String.class,
                prefix + "-a")).isEqualTo("CALIBRATED");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM calibration_version WHERE calibration_version LIKE ?", Integer.class,
                prefix + "%")).isEqualTo(1);
    }

    @Test
    void 새_버전은_이전_버전을_가리키고_재보정_뒤_현재가_된다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "CALIBRATED", "INITIAL", base));
            awaitCount(() -> messages(prefix + "-a"), 1);
            ObjectNode next = update(prefix + "-b", prefix + "-a", "CALIBRATED", "RECALIBRATED", base + 1_000);
            ((ObjectNode) next.get("payload")).put("calibration_hash", "sha256:" + "2".repeat(64));
            tier0.send(next);
        }

        awaitCount(() -> messages(prefix + "-b"), 1);
        JsonNode current = call("/api/calibrations/current", viewer(), 200);
        assertThat(current.get("calibration_version").asString()).isEqualTo(prefix + "-b");
        assertThat(current.get("previous_calibration_version").asString()).isEqualTo(prefix + "-a");
        assertThat(jdbc.queryForObject("SELECT previous_calibration_version FROM calibration_version WHERE calibration_version = ?",
                String.class, prefix + "-b")).isEqualTo(prefix + "-a");
    }

    @Test
    void 같은_msg_id_는_한_번만_저장하고_같은_버전의_다른_해시와_계약_위반은_버린다() throws Exception {
        ObjectNode first = update(prefix + "-a", null, "CALIBRATED", "INITIAL", base);
        ObjectNode otherHash = update(prefix + "-a", null, "DRIFTED", "DRIFT_DETECTED", base + 1_000);
        ((ObjectNode) otherHash.get("payload")).put("calibration_hash", "sha256:" + "3".repeat(64));
        // 공개 데이터 정답 보정은 CALIBRATED 가 될 수 없다(계약)
        ObjectNode simCalibrated = update(prefix + "-a", null, "CALIBRATED", "STATE_CHANGED", base + 2_000);
        ((ObjectNode) simCalibrated.get("payload")).put("provider_source", "SIM");
        double duplicateBefore = counter("duplicate");
        double invalidBefore = counter("invalid");

        try (Tier0 tier0 = tier0()) {
            tier0.send(first);
            tier0.send(first);
            tier0.send(otherHash);
            tier0.send(simCalibrated);
        }

        awaitCount(() -> (int) (counter("invalid") - invalidBefore), 2);
        assertThat(counter("duplicate") - duplicateBefore).isEqualTo(1.0);
        assertThat(messages(prefix + "-a")).isEqualTo(1);
        assertThat(call("/api/calibrations/current", viewer(), 200).get("calibration_state").asString()).isEqualTo("CALIBRATED");
    }

    @Test
    void Tier0_가_알린_버전에_같은_내용의_문서를_올리면_받고_다른_내용은_해시를_맞춰_적어도_거부한다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "CALIBRATED", "INITIAL", base));
            ObjectNode other = update(prefix + "-b", prefix + "-a", "CALIBRATED", "RECALIBRATED", base + 1_000);
            ((ObjectNode) other.get("payload")).put("calibration_hash", "sha256:" + "4".repeat(64));
            tier0.send(other);
        }
        awaitCount(() -> messages(prefix + "-b"), 1);
        assertThat(call("/api/calibrations/current", viewer(), 200).get("document_uploaded").asBoolean()).isFalse();

        // 원문 해시가 아니라 다시 잰 content_hash(버전·시각·상태를 뺀 내용)로 맞춘다. 문서가 적은 content_hash 는 무시한다
        call("POST", "/api/calibrations", admin(), upload(document(prefix + "-a", "sha256:" + "9".repeat(64))), 201);
        // 내용이 다른 문서가 Tier 0 의 해시를 적어 와도 다시 재서 거부한다
        assertThat(call("POST", "/api/calibrations", admin(), upload(document(prefix + "-b", "sha256:" + "4".repeat(64))), 409)
                .get("code").asString()).isEqualTo("CALIBRATION_VERSION_CONFLICT");
        JsonNode current = call("/api/calibrations/current", viewer(), 200);
        assertThat(current.get("calibration_version").asString()).isEqualTo(prefix + "-b");
        assertThat(current.get("document_uploaded").asBoolean()).isFalse();
        assertThat(current.get("latest_uploaded_version").asString()).isEqualTo(prefix + "-a");
    }

    @Test
    void 업로드로_먼저_만든_버전은_이전_버전을_비워_두고_Tier0_알림이_채운다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "CALIBRATED", "INITIAL", base));
        }
        awaitCount(() -> messages(prefix + "-a"), 1);
        ObjectNode doc = document(prefix + "-b", null);
        ((ObjectNode) doc.get("cameras").get(0)).put("camera_height_m", 2.75);
        call("POST", "/api/calibrations", admin(), upload(doc), 201);
        assertThat(previous(prefix + "-b")).isNull();
        String uploadedHash = jdbc.queryForObject("SELECT calibration_hash FROM calibration_version WHERE calibration_version = ?",
                String.class, prefix + "-b");

        try (Tier0 tier0 = tier0()) {
            ObjectNode next = update(prefix + "-b", prefix + "-a", "PROVISIONAL", "RECALIBRATED", base + 1_000);
            ((ObjectNode) next.get("payload")).put("calibration_hash", uploadedHash);
            tier0.send(next);
        }

        awaitCount(() -> messages(prefix + "-b"), 1);
        assertThat(previous(prefix + "-b")).isEqualTo(prefix + "-a");
        // 처음 만든 값(업로드한 문서의 상태·주소)은 그대로다
        assertThat(jdbc.queryForObject("SELECT calibration_ref FROM calibration_version WHERE calibration_version = ?",
                String.class, prefix + "-b")).isEqualTo("ec2:calibration_document/" + prefix + "-b");
        assertThat(call("/api/calibrations/current", viewer(), 200).get("document_uploaded").asBoolean()).isTrue();
    }

    @Test
    void 예전에_원문_해시로_남긴_업로드_행은_같은_내용이면_해시를_바로잡고_받는다() throws Exception {
        String version = prefix + "-a";
        byte[] content = MAPPER.writeValueAsBytes(document(version, null));
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, 'classroom-1', 'PROVISIONAL', 'G1_AUTO', '관리자 업로드', now(), ?, 'sha256:raw-bytes', now())
                """, version, "ec2:calibration_document/" + version);
        jdbc.update("""
                INSERT INTO calibration_document (calibration_version, content, sha256, uploaded_by, uploaded_at)
                VALUES (?, ?, 'sha256:raw-bytes', ?, now())
                """, version, content, adminId);

        try (Tier0 tier0 = tier0()) {
            tier0.send(update(version, null, "CALIBRATED", "STATE_CHANGED", base));
        }

        awaitCount(() -> messages(version), 1);
        assertThat(jdbc.queryForObject("SELECT calibration_hash FROM calibration_version WHERE calibration_version = ?",
                String.class, version)).isEqualTo(EXAMPLE_HASH);
        assertThat(call("/api/calibrations/current", viewer(), 200).get("calibration_state").asString()).isEqualTo("CALIBRATED");
    }

    @Test
    void 설치_안내는_현재_보정_버전의_안내를_관리자에게만_준다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "PROVISIONAL", "INITIAL", base));
            awaitCount(() -> messages(prefix + "-a"), 1);
            ObjectNode next = update(prefix + "-b", prefix + "-a", "CALIBRATED", "RECALIBRATED", base + 1_000);
            ((ObjectNode) next.get("payload")).put("calibration_hash", "sha256:" + "5".repeat(64));
            tier0.send(next);
        }
        awaitCount(() -> messages(prefix + "-b"), 1);
        guide(prefix + "-a", "cam1", "이전 보정의 안내");
        guide(prefix + "-b", "cam3", "카메라 3번이 너무 아래를 봄");
        guide(prefix + "-b", null, "남쪽 벽을 따라 한 번 더 걸어 주세요");

        JsonNode body = call("/api/cameras/coverage-guides", admin(), 200);

        assertThat(body.get("calibration_version").asString()).isEqualTo(prefix + "-b");
        assertThat(body.get("guides")).hasSize(2);
        assertThat(body.get("guides").get(0).get("camera_id").asString()).isEqualTo("cam3");
        assertThat(body.get("guides").get(0).get("message").asString()).isEqualTo("카메라 3번이 너무 아래를 봄");
        assertThat(body.get("guides").get(0).at("/metrics/floor_coverage_ratio").asDouble()).isEqualTo(0.42);
        assertThat(body.get("guides").get(1).get("camera_id").isNull()).isTrue();
        call("/api/cameras/coverage-guides", viewer(), 403);
    }

    // ---- 도우미

    /** 계약 예시 calibration_update 를 이 시험의 버전·Tier 0·시각으로 바꾼다. effective_at 은 sent_at + 600 */
    private ObjectNode update(String version, String previous, String state, String reason, long sentAt) throws Exception {
        ObjectNode root = read("calibration_update.json");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", sentAt);
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("calibration_version", version);
        p.put("previous_calibration_version", previous);
        p.put("calibration_state", state);
        p.put("reason", reason);
        p.put("effective_at", sentAt + 600);
        p.put("calibration_hash", EXAMPLE_HASH);
        return root;
    }

    /** 계약 예시 보정 문서. claimedHash 가 있으면 문서의 content_hash 칸에 적는다(EC2 는 믿지 않고 다시 잰다) */
    private ObjectNode document(String version, String claimedHash) throws Exception {
        ObjectNode doc = read("calibration.json");
        doc.put("calibration_version", version);
        if (claimedHash != null) {
            doc.put("content_hash", claimedHash);
        }
        return doc;
    }

    private String previous(String version) {
        return jdbc.queryForObject("SELECT previous_calibration_version FROM calibration_version WHERE calibration_version = ?",
                String.class, version);
    }

    private static ObjectNode upload(ObjectNode doc) {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("reason", "시험 보정");
        body.set("document", doc);
        return body;
    }

    private void guide(String version, String cameraId, String message) {
        jdbc.update("""
                INSERT INTO coverage_guide (calibration_version, camera_id, message, metrics, created_at)
                VALUES (?, ?, ?, '{"floor_coverage_ratio": 0.42}'::jsonb, now())
                """, version, cameraId, message);
    }

    private static ObjectNode read(String name) throws Exception {
        return (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples").resolve(name)));
    }

    private int messages(String version) {
        return jdbc.queryForObject("SELECT count(*) FROM calibration_update_message WHERE calibration_version = ?",
                Integer.class, version);
    }

    /** 지표는 앱 전체에서 누적된다. 시험은 보내기 전후 차이만 본다 */
    private double counter(String result) {
        Counter counter = meters.find(METRIC).tags("msg_type", "calibration_update", "result", result).counter();
        return counter == null ? 0 : counter.count();
    }

    private static void awaitCount(Supplier<Integer> actual, int expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (actual.get() < expected && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(actual.get()).isEqualTo(expected);
    }

    private JsonNode call(String path, String token, int expectedStatus) throws Exception {
        return call("GET", path, token, null, expectedStatus);
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

    private String viewer() {
        return token("VIEWER");
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
