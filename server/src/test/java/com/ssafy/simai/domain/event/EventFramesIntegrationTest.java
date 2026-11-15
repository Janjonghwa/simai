package com.ssafy.simai.domain.event;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.UUID;
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
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * 이벤트 프레임(사진) 목록 GET /api/events/{id}/frames (S15P31A101-753). 근거 작업 입력의 frames 에서 읽어 GET 서명 주소를 낸다.
 * 저장소는 서명 계산만 하므로 실제 버킷 없이 돈다(application-test.yaml, 버킷 simai-test-bucket).
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class EventFramesIntegrationTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final String BUCKET = "s3://simai-test-bucket/";

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    private final HttpClient http = HttpClient.newHttpClient();
    private final UUID eventId = UUID.randomUUID();
    private final String jobId = "job-frames-" + UUID.randomUUID();

    @BeforeEach
    void setUp() {
        Instant raised = Instant.parse("2026-04-01T00:00:00Z");
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity,
                    confidence, needs_check, world_ids, zone_id, metrics, resume_count, event_sequence, raised_at, updated_at,
                    delivery, retention_hold)
                VALUES (?, 'LIVE', 'tier0-frames', 'R1', 1, 'RAISED', 'VERIFIED', 'CRITICAL', 'HIGH', false, '{w-1}', NULL, '{}',
                    0, 1, ?, ?, 'REALTIME', false)
                """, eventId, Timestamp.from(raised), Timestamp.from(raised));
    }

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM analysis_job WHERE job_id = ?", jobId);
        jdbc.update("DELETE FROM risk_event WHERE event_id = ?", eventId);
    }

    @Test
    void 근거_작업이_없으면_빈_목록() throws Exception {
        JsonNode body = get("/api/events/" + eventId + "/frames", "ADMIN", 200);
        assertThat(body.get("event_id").asString()).isEqualTo(eventId.toString());
        assertThat(body.get("frames")).isEmpty();
    }

    @Test
    void 프레임을_촬영_시각_순으로_서명_주소와_함께_주고_다른_이벤트_객체는_내지_않는다() throws Exception {
        String mine = BUCKET + "event-frames/" + eventId + "/";
        String frames = """
                [{"camera_id":"cam_B","frame_uri":"%sb.jpg","estimated_capture_time_ms":2000,"timestamp_source":"RECEIVE_ESTIMATED","timestamp_uncertainty_ms":20,
                  "local_copy":{"path":"event-frames/x/b.jpg","sha256":"%s","size_bytes":9},"face_policy":"BLURRED"},
                 {"camera_id":"cam_A","frame_uri":"%sa.jpg","estimated_capture_time_ms":1000,"timestamp_source":"RECEIVE_ESTIMATED","timestamp_uncertainty_ms":20},
                 {"camera_id":"cam_C","frame_uri":"%sevent-frames/%s/c.jpg","estimated_capture_time_ms":3000,"timestamp_source":"RECEIVE_ESTIMATED","timestamp_uncertainty_ms":20},
                 {"camera_id":"cam_D","frame_uri":"s3://other-bucket/event-frames/%s/d.jpg","estimated_capture_time_ms":4000,"timestamp_source":"RECEIVE_ESTIMATED","timestamp_uncertainty_ms":20}]
                """.formatted(mine, "0".repeat(64), mine, BUCKET, UUID.randomUUID(), eventId);
        jdbc.update("""
                INSERT INTO analysis_job (job_id, job_type, priority, status, attempt, max_attempts, timeout_ms, input, event_id, created_at)
                VALUES (?, 'vlm_rationale', 'VLM_FAST', 'SUCCEEDED', 1, 2, 5000, ('{"frames":' || ? || '}')::jsonb, ?, now())
                """, jobId, frames, eventId);

        JsonNode list = get("/api/events/" + eventId + "/frames", "ADMIN", 200).get("frames");
        assertThat(list).hasSize(2);
        assertThat(list.get(0).get("camera_id").asString()).isEqualTo("cam_A");
        assertThat(list.get(0).get("capture_ms").asLong()).isEqualTo(1000);
        assertThat(list.get(0).get("url").asString()).contains("event-frames/" + eventId + "/a.jpg").contains("X-Amz-Signature");
        assertThat(list.get(0).get("expires_at_ms").asLong()).isGreaterThan(System.currentTimeMillis());
        assertThat(list.get(1).get("camera_id").asString()).isEqualTo("cam_B");
        assertThat(list.get(1).has("local_copy")).isFalse();
    }

    @Test
    void 관리자만_보고_없는_이벤트는_404() throws Exception {
        get("/api/events/" + eventId + "/frames", "FOREMAN", 403);
        get("/api/events/" + eventId + "/frames", "VIEWER", 403);
        assertThat(get("/api/events/" + UUID.randomUUID() + "/frames", "ADMIN", 404).get("code").asString())
                .isEqualTo("EVENT_NOT_FOUND");
    }

    private JsonNode get(String path, String role, int expectedStatus) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt(role)).GET().build();
        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).as(path + " → " + response.body()).isEqualTo(expectedStatus);
        return response.body().isEmpty() ? MAPPER.nullNode() : MAPPER.readTree(response.body());
    }

    private String jwt(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject("1").issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }
}
