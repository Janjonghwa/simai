package com.ssafy.simai.domain.tier0.event;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * Tier 0 가 사라진 뒤의 열린 LIVE 이벤트 마감(S15P31A101-722). GPU 세션·이벤트는 DB 에 직접 넣고 OrphanEventSweeper 를 부른다.
 * 이 시험의 gateway_id 에는 I1 연결이 없다(살아 있는 Tier 0 없음).
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class OrphanEventSweeperIntegrationTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    OrphanEventSweeper sweeper;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String gatewayId = "tier0-orphan-" + suffix;
    private final String workerId = "worker-orphan-" + suffix;
    private final String bootId = "boot-orphan-" + suffix;

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM risk_event WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM gpu_session_event WHERE session_id IN (SELECT session_id FROM gpu_session WHERE gateway_id = ?)",
                gatewayId);
        jdbc.update("DELETE FROM gpu_session WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM gpu_boot_secret WHERE boot_id = ?", bootId);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", workerId);
    }

    @Test
    void GPU_세션이_모두_끝나면_열린_이벤트를_SESSION_ENDED_로_닫고_해소된_이벤트는_건드리지_않는다() throws Exception {
        UUID session = session();
        end(session, "END_REPORTED");
        UUID open = event("UPDATED", 3);
        UUID resolved = event("RESOLVED", 2);

        assertThat(sweeper.closeEndedSessions()).isGreaterThanOrEqualTo(1);

        assertThat(column(open, "lifecycle")).isEqualTo("RESOLVED");
        assertThat(column(open, "verification")).isEqualTo("UNVERIFIABLE");
        assertThat(column(open, "severity")).isEqualTo("HIGH");
        JsonNode resolution = MAPPER.readTree(column(open, "resolution"));
        assertThat(resolution.path("kind").asString()).isEqualTo("SESSION_ENDED");
        assertThat(resolution.path("trigger_ref").asString()).isEqualTo(session.toString());
        assertThat(resolution.path("last_event_sequence").asInt()).isEqualTo(3);
        assertThat(resolution.path("reason").asString()).contains("END_REPORTED");
        assertThat(resolution.path("closed_at").isNumber()).isTrue();
        assertThat(jdbc.queryForObject("SELECT lifecycle_before FROM risk_event_server_close WHERE event_id = ?",
                String.class, open)).isEqualTo("UPDATED");

        assertThat(column(resolved, "resolution")).isNull();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM risk_event_server_close WHERE event_id = ?", Integer.class, resolved))
                .isZero();

        // 다시 돌려도 이미 닫힌 이벤트에 기록을 더하지 않는다
        sweeper.closeEndedSessions();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM risk_event_server_close WHERE event_id = ?", Integer.class, open))
                .isEqualTo(1);
    }

    @Test
    void 열린_GPU_세션이_있으면_세션_종료로도_작업_종료로도_닫지_않는다() {
        UUID ended = session();
        end(ended, "SUPERSEDED");
        session(); // 새 세션(아직 열림): 그 Tier 0 가 붙어 스냅샷을 보내거나 끝날 때 닫힌다
        UUID open = event("RAISED", 1);

        sweeper.closeEndedSessions();
        sweeper.closeAfterWorkEnd(1L);

        assertThat(column(open, "lifecycle")).isEqualTo("RAISED");
        assertThat(column(open, "resolution")).isNull();
    }

    @Test
    void 작업_종료_때_연결도_GPU_세션도_없는_Tier0_의_열린_이벤트는_WORK_ENDED_로_닫는다() throws Exception {
        UUID open = event("RAISED", 1);

        sweeper.closeAfterWorkEnd(42L);

        assertThat(column(open, "lifecycle")).isEqualTo("RESOLVED");
        JsonNode resolution = MAPPER.readTree(column(open, "resolution"));
        assertThat(resolution.path("kind").asString()).isEqualTo("WORK_ENDED");
        assertThat(resolution.path("trigger_ref").asString()).isEqualTo("42");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM risk_event_update WHERE event_id = ?", Integer.class, open))
                .isZero();
    }

    // ---- 도우미

    private UUID session() {
        if (jdbc.queryForObject("SELECT count(*) FROM worker WHERE worker_id = ?", Integer.class, workerId) == 0) {
            jdbc.update("INSERT INTO worker (worker_id, token_hash, job_types, issued_at) VALUES (?, ?, '{}', now())",
                    workerId, "hash-" + suffix);
            jdbc.update("INSERT INTO gpu_boot_secret (boot_id, token_hash, worker_id, issued_at) VALUES (?, ?, ?, now())",
                    bootId, "boot-hash-" + suffix, workerId);
        }
        UUID id = UUID.randomUUID();
        jdbc.update("""
                INSERT INTO gpu_session (session_id, boot_id, provider, instance_id, gpu_type, public_host, ports, worker_id,
                    gateway_id, tier0_token_hash, registered_at, register_msg_id)
                VALUES (?, ?, 'MANUAL', 'i-1', 'H100', '127.0.0.1', '{}'::jsonb, ?, ?, ?, ?, ?)
                """, id, bootId, workerId, gatewayId, "t0-" + id, Timestamp.from(Instant.now()), UUID.randomUUID());
        jdbc.update("INSERT INTO gpu_session_event (session_id, kind, received_at) VALUES (?, 'REGISTERED', now())", id);
        return id;
    }

    private void end(UUID session, String reason) {
        jdbc.update("INSERT INTO gpu_session_event (session_id, kind, end_reason, received_at) VALUES (?, 'ENDED', ?, now())",
                session, reason);
    }

    private UUID event(String lifecycle, int sequence) {
        UUID id = UUID.randomUUID();
        Timestamp at = Timestamp.from(Instant.now());
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification,
                    severity, confidence, needs_check, world_ids, zone_id, metrics, resume_count, event_sequence,
                    raised_at, updated_at, resolved_at, delivery, retention_hold)
                VALUES (?, 'LIVE', ?, 'R2', 1, ?, 'UNVERIFIABLE', 'HIGH', 'LOW', true, '{w-1}', 'z-1', '{}', 0, ?, ?, ?, ?,
                    'REALTIME', false)
                """, id, gatewayId, lifecycle, sequence, at, at, "RESOLVED".equals(lifecycle) ? at : null);
        return id;
    }

    private String column(UUID eventId, String column) {
        return jdbc.queryForObject("SELECT " + column + " FROM risk_event WHERE event_id = ?", String.class, eventId);
    }
}
