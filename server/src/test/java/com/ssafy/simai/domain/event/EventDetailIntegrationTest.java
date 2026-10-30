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
 * 이벤트 상세(GET /api/events/{id}, 명세 12.6, 히스토리 S15P31A101-320). 이벤트·전이·구역은 이 시험만의 ID 로 바로 넣고 지운다.
 * 구역은 이벤트가 난 시각에 유효했던 판을 준다(뒤에 고친 판이 아니다).
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class EventDetailIntegrationTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String zoneId = "zone-detail-" + tag;
    private final UUID eventId = uuid7();
    private final Instant raised = Instant.parse("2026-04-01T00:00:00Z").plusSeconds((long) (Math.random() * 1_000_000));

    @BeforeEach
    void setUp() {
        String site = jdbc.queryForObject("SELECT site_id FROM site", String.class);
        // 판 1 은 이벤트 시각에 유효, 판 2 는 그 뒤에 고친 판
        zone(site, 1, "옛 이름", raised.minusSeconds(3600), raised.plusSeconds(60));
        zone(site, 2, "새 이름", raised.plusSeconds(60), null);
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity,
                    confidence, needs_check, world_ids, zone_id, metrics, resume_count, event_sequence, raised_at, updated_at,
                    delivery, retention_hold)
                VALUES (?, 'LIVE', 'tier0-detail', 'R1', 1, 'UPDATED', 'VERIFIED', 'HIGH', 'HIGH', false, '{w-1}', ?, '{}', 0, 2,
                    ?, ?, 'DELAYED_ACTIVE', false)
                """, eventId, zoneId, Timestamp.from(raised), Timestamp.from(raised.plusSeconds(5)));
        transition(1, "RAISED", raised, "AUDIT_ONLY");
        transition(2, "UPDATED", raised.plusSeconds(5), "REALTIME");
    }

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM risk_event_update WHERE event_id = ?", eventId);
        jdbc.update("DELETE FROM risk_event WHERE event_id = ?", eventId);
        jdbc.update("DELETE FROM zone WHERE zone_id = ?", zoneId);
    }

    @Test
    void 상세는_목록_칸에_그때의_구역과_전이_이력을_더한다() throws Exception {
        JsonNode detail = get("/api/events/" + eventId, "FOREMAN", 200);
        assertThat(detail.get("event_id").asString()).isEqualTo(eventId.toString());
        assertThat(detail.get("lifecycle").asString()).isEqualTo("UPDATED");
        // 재시작 복원 표시(계약 delivery)
        assertThat(detail.get("delivery").asString()).isEqualTo("DELAYED_ACTIVE");
        assertThat(detail.get("acks")).isEmpty();
        assertThat(detail.at("/zone/name").asString()).isEqualTo("옛 이름");
        assertThat(detail.at("/zone/zone_version").asInt()).isEqualTo(1);
        JsonNode transitions = detail.get("transitions");
        assertThat(transitions).hasSize(2);
        assertThat(transitions.get(0).get("transition").asString()).isEqualTo("RAISED");
        assertThat(transitions.get(0).get("delivery").asString()).isEqualTo("AUDIT_ONLY");
        assertThat(transitions.get(0).get("occurred_at").asLong()).isEqualTo(raised.toEpochMilli());
        assertThat(transitions.get(1).get("event_sequence").asInt()).isEqualTo(2);
        assertThat(transitions.get(1).has("ack_id")).isFalse();
    }

    @Test
    void 반장_관리자만_보고_없는_이벤트는_404() throws Exception {
        get("/api/events/" + eventId, "ADMIN", 200);
        get("/api/events/" + eventId, "VIEWER", 403);
        assertThat(get("/api/events/" + uuid7(), "ADMIN", 404).get("code").asString()).isEqualTo("EVENT_NOT_FOUND");
        // 구역이 없는 이벤트는 zone null
        jdbc.update("UPDATE risk_event SET zone_id = NULL WHERE event_id = ?", eventId);
        assertThat(get("/api/events/" + eventId, "ADMIN", 200).get("zone").isNull()).isTrue();
    }

    private void zone(String site, int version, String name, Instant from, Instant to) {
        jdbc.update("""
                INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from, valid_to)
                VALUES (?, ?, ?, ?, 'OPENING', 'GROUND_FOOTPRINT', ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 0))', 0), 'HIGH', ?, ?)
                """, zoneId, version, site, name, Timestamp.from(from), to == null ? null : Timestamp.from(to));
    }

    private void transition(int sequence, String kind, Instant at, String deliveryMode) {
        Timestamp t = Timestamp.from(at);
        jdbc.update("""
                INSERT INTO risk_event_update (event_id, event_sequence, msg_id, gateway_id, journal_offset, transition, occurred_at,
                    observation_at, evaluated_at, rule_id, rule_version, config_version, calibration_version, scene_version, source,
                    severity, confidence, verification, needs_check, world_ids, zone_id, metrics, resent, resumed, delivery_mode,
                    received_at)
                VALUES (?, ?, ?, 'tier0-detail', ?, ?, ?, ?, ?, 'R1', 1, 'cfg-1', 'cal-1', 'scene-1', 'LIVE', 'HIGH', 'HIGH', 'VERIFIED',
                    false, '{w-1}', ?, '{}', false, false, ?, ?)
                """, eventId, sequence, UUID.randomUUID(), (long) (Math.random() * 1_000_000_000L), kind, t, t, t, zoneId,
                deliveryMode, t);
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

    private static UUID uuid7() {
        UUID random = UUID.randomUUID();
        long high = (System.currentTimeMillis() << 16) | 0x7000L | (random.getMostSignificantBits() & 0x0FFFL);
        long low = (random.getLeastSignificantBits() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
        return new UUID(high, low);
    }
}
