package com.ssafy.simai.domain.metric;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;
import org.junit.jupiter.api.AfterEach;
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
 * Tier 0 health 지표 저장·조회(S15P31A101-70). 실제 I1(WebSocket)으로 health 를 보내 system_metric 에 풀린 지표를 GET /api/metrics 로 읽는다.
 * 시험마다 다른 gateway_id·과거 시각을 써서 다른 시험·이전 실행의 행과 섞이지 않게 하고 끝나면 지운다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class SystemMetricIntegrationTest {

    /** application-test.yaml 의 simai.tier0.token-sha256 원문 */
    private static final String TOKEN = "test-only-tier0-token";
    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    /**
     * 계약 예시 health.json 하나에서 나오는 지표 수. 전체 7(pipeline_lag·capture_to_risk·gpu 둘·송신 버림 둘·시계 차이, 하한 지연은 예시에 없음),
     * cam_A 13(fps·sync_offset·timestamp_uncertainty·frame_age·queue_wait·max_result_gap + frames 7), cam_B 1(fps 0, 나머지는 null·없음)
     */
    private static final int PER_HEALTH = 7 + 13 + 1;

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    Ec2MetricSampler sampler;

    private final String gatewayId = "tier0-metric-" + UUID.randomUUID();
    /** 최근 시각(압축 7일·보관 30일 정책 밖의 오래된 청크를 건드리지 않게). 시험마다 다른 초로 흩는다 */
    private final long baseMs = Instant.now().minus(Duration.ofHours(2)).toEpochMilli() - (long) (Math.random() * 3_600_000L);
    private final HttpClient http = HttpClient.newHttpClient();

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM system_metric WHERE source_id = ?", gatewayId);
        jdbc.update("DELETE FROM system_health WHERE gateway_id = ?", gatewayId);
    }

    @Test
    void health_를_받으면_지표가_system_metric_에_풀리고_관리자가_계열로_조회한다() throws Exception {
        try (Session session = connect()) {
            session.send(health(baseMs));
            session.send(health(baseMs + 1000));
        }
        awaitRows(2 * PER_HEALTH);

        Map<String, Double> first = jdbc.queryForList("""
                SELECT metric || '/' || camera_id AS k, value FROM system_metric
                WHERE source_id = ? AND measured_at = to_timestamp(? / 1000.0)
                """, gatewayId, baseMs).stream()
                .collect(Collectors.toMap(r -> (String) r.get("k"), r -> ((Number) r.get("value")).doubleValue()));
        // 계약 예시 health.json 의 값
        assertThat(first).hasSize(PER_HEALTH)
                .containsEntry("pipeline_lag_ms/", 120.0).containsEntry("capture_to_risk_ms/", 480.0)
                .containsEntry("gpu_util/", 0.71).containsEntry("gpu_memory_used_ratio/", 0.62)
                .containsEntry("ec2_world_state_drops_disconnected/", 41.0).containsEntry("laptop_clock_offset_ms/", -12.5)
                .containsEntry("fps/cam_A", 14.8).containsEntry("frame_age_ms/cam_A", 210.0).containsEntry("queue_wait_ms/cam_A", 40.0)
                .containsEntry("frames_received/cam_A", 25.0).containsEntry("camera_to_gateway_gap_max_ms/cam_A", 80.0)
                // 받지 못한 카메라의 FPS 0 은 쓰고, 잴 수 없는(null) 동기 오프셋은 쓰지 않는다
                .containsEntry("fps/cam_B", 0.0).doesNotContainKey("sync_offset_ms/cam_B").doesNotContainKey("frame_age_ms/cam_B");
        assertThat(jdbc.queryForObject("""
                SELECT labels->>'p50' FROM system_metric WHERE source_id = ? AND metric = 'capture_to_risk_ms' ORDER BY measured_at LIMIT 1
                """, String.class, gatewayId)).isEqualTo("310");

        JsonNode body = get("/api/metrics?source_id=" + gatewayId + "&metric=pipeline_lag_ms,fps&from=" + (baseMs - 1) + "&to="
                + (baseMs + 2000), "ADMIN", 200);
        assertThat(body.get("truncated").booleanValue()).isFalse();
        // (출처, 지표, 카메라) 순: fps/cam_A, fps/cam_B, pipeline_lag_ms
        assertThat(body.get("series")).hasSize(3);
        JsonNode lag = body.get("series").get(2);
        assertThat(lag.get("metric").stringValue()).isEqualTo("pipeline_lag_ms");
        assertThat(lag.get("camera_id").isNull()).isTrue();
        assertThat(lag.get("unit").stringValue()).isEqualTo("ms");
        assertThat(lag.get("points")).hasSize(2);
        assertThat(lag.get("points").get(0).get("t").longValue()).isEqualTo(baseMs);
        assertThat(body.get("series").get(0).get("camera_id").stringValue()).isEqualTo("cam_A");

        // 상태 패널: 계열마다 가장 늦은 한 점
        JsonNode latest = get("/api/metrics?source_id=" + gatewayId + "&camera_id=cam_A&latest=true&from=" + (baseMs - 1) + "&to="
                + (baseMs + 2000), "ADMIN", 200);
        assertThat(latest.get("series")).hasSize(13);
        for (JsonNode series : latest.get("series")) {
            assertThat(series.get("camera_id").stringValue()).isEqualTo("cam_A");
            assertThat(series.get("points")).hasSize(1);
            assertThat(series.get("points").get(0).get("t").longValue()).isEqualTo(baseMs + 1000);
        }
        // 빈 필터는 필터 없음이다(카메라별 계열이 사라지지 않는다)
        JsonNode blankCamera = get("/api/metrics?source_id=" + gatewayId + "&camera_id=&metric=fps&from=" + (baseMs - 1) + "&to="
                + (baseMs + 2000), "ADMIN", 200);
        assertThat(blankCamera.get("series")).hasSize(2);
        JsonNode frameAge = latest.get("series").valueStream().filter(s -> s.get("metric").stringValue().equals("frame_age_ms"))
                .findFirst().orElseThrow();
        assertThat(frameAge.get("points").get(0).get("labels").get("p50").intValue()).isEqualTo(120);
    }

    @Test
    void 같은_health_재전송은_지표를_두_번_쓰지_않는다() throws Exception {
        ObjectNode health = health(baseMs);
        try (Session session = connect()) {
            session.send(health);
            session.send(health);
            session.send(health(baseMs + 1000));
        }
        awaitRows(2 * PER_HEALTH);
        Thread.sleep(300);
        assertThat(rows()).isEqualTo(2 * PER_HEALTH);
    }

    @Test
    void 관리자만_조회하고_기간이_맞지_않으면_400() throws Exception {
        get("/api/metrics", "VIEWER", 403);
        get("/api/metrics", "FOREMAN", 403);
        get("/api/metrics", null, 401);
        get("/api/metrics?from=" + (baseMs + 10) + "&to=" + baseMs, "ADMIN", 400);
        // 숫자가 아니거나 DB 가 받지 못하는 시각도 코드가 있는 400(500·코드 없는 400 이 아니게)
        assertThat(get("/api/metrics?from=abc", "ADMIN", 400).get("code").stringValue()).isEqualTo("INVALID_METRIC_QUERY");
        assertThat(get("/api/metrics?from=9000000000000000&to=9000000000000001", "ADMIN", 400).get("code").stringValue())
                .isEqualTo("INVALID_METRIC_QUERY");
        get("/api/metrics?from=-1&to=0", "ADMIN", 400);
        assertThat(get("/api/metrics?from=" + baseMs + "&to=" + (baseMs + Duration.ofHours(25).toMillis()), "ADMIN", 400)
                .get("code").stringValue()).isEqualTo("INVALID_METRIC_QUERY");
        // 기본 기간: 최근 10분, latest 는 1시간
        JsonNode body = get("/api/metrics?source_id=" + gatewayId, "ADMIN", 200);
        assertThat(body.get("to").longValue() - body.get("from").longValue()).isEqualTo(Duration.ofMinutes(10).toMillis());
        assertThat(body.get("series")).isEmpty();
        JsonNode latest = get("/api/metrics?latest=true&source_id=" + gatewayId, "ADMIN", 200);
        assertThat(latest.get("to").longValue() - latest.get("from").longValue()).isEqualTo(Duration.ofHours(1).toMillis());
    }

    @Test
    void EC2_자체_지표는_Micrometer_에서_읽는다() {
        List<SystemMetricRepository.Row> rows = sampler.rows(Instant.ofEpochMilli(baseMs));
        Map<String, Double> values = rows.stream().collect(Collectors.toMap(SystemMetricRepository.Row::metric,
                SystemMetricRepository.Row::value));
        assertThat(rows).allSatisfy(row -> {
            assertThat(row.sourceId()).isEqualTo("ec2");
            assertThat(row.cameraId()).isEmpty();
        });
        assertThat(values).containsKeys("ec2_process_cpu_usage", "ec2_jvm_heap_used_bytes", "ec2_jvm_heap_used_ratio");
        assertThat(values.get("ec2_jvm_heap_used_ratio")).isBetween(0.0, 1.0);
    }

    private ObjectNode health(long sentAtMs) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("health.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        root.put("sent_at", sentAtMs);
        ((ObjectNode) root.get("payload")).put("t_latest_global", sentAtMs);
        return root;
    }

    private int rows() {
        return jdbc.queryForObject("SELECT count(*) FROM system_metric WHERE source_id = ?", Integer.class, gatewayId);
    }

    private void awaitRows(int expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (rows() < expected && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(rows()).isEqualTo(expected);
    }

    private Session connect() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TOKEN);
        return new Session(new StandardWebSocketClient().execute(new AbstractWebSocketHandler() { }, headers,
                URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS));
    }

    private JsonNode get(String path, String role, int expectedStatus) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path)).GET();
        if (role != null) {
            request.header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt(role));
        }
        HttpResponse<String> response = http.send(request.build(), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).as(response.body()).isEqualTo(expectedStatus);
        return response.body().isEmpty() ? MAPPER.createObjectNode() : MAPPER.readTree(response.body());
    }

    private String jwt(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject("1").issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private record Session(WebSocketSession session) implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(MAPPER.writeValueAsString(message)));
        }

        @Override
        public void close() throws Exception {
            session.close();
        }
    }
}
