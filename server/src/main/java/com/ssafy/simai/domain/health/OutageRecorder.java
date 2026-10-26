package com.ssafy.simai.domain.health;

import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Objects;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;

/**
 * 장애(관제 정지·현장 장비 응답 없음·원격 전달 단절, FR-11)가 시작·바뀜·끝날 때 system_outage 에 남기고 로그를 쓴다(명세 6.5, G3).
 * 화면 알림은 viewer_health(/topic/health, 바뀌면 즉시)가 하고, 이 기록은 "단절 10초 안에 관리자 화면 알림"(G3)을 나중에 재는 근거다:
 * last_seen_at(실패한 쪽의 마지막 정상 근거) → detected_at(EC2 가 장애로 본 시각).
 * HealthPublisher 의 전용 스레드에서만 부른다. EC2 가 막 떠서 아직 아무것도 받지 못했을 때(linkTimeoutMs 안)는 기록하지 않는다
 * (재시작을 장애로 남기지 않는다). 뜰 때 닫히지 않은 기록은 EC2_RESTART 로 닫는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class OutageRecorder {

    private static final Logger log = LoggerFactory.getLogger(OutageRecorder.class);

    private final NamedParameterJdbcTemplate jdbc;
    private final HealthState state;
    private final Clock clock;
    private final long startNanos;
    private final long graceNanos;
    private String current;
    private Long openId;

    OutageRecorder(NamedParameterJdbcTemplate jdbc, HealthState state, ViewerHealthService health, Clock clock) {
        this.jdbc = jdbc;
        this.state = state;
        this.clock = clock;
        this.startNanos = state.nanoTime();
        this.graceNanos = health.linkTimeoutMs() * 1_000_000L;
        int closed = jdbc.update("UPDATE system_outage SET ended_at = :now, end_reason = 'EC2_RESTART' WHERE ended_at IS NULL",
                new MapSqlParameterSource("now", utc(clock.instant())));
        if (closed > 0) {
            log.info("EC2 재시작 전 열린 장애 기록을 닫음: {}건", closed);
        }
    }

    void observe(JsonNode payload) {
        if (state.nanoTime() - startNanos <= graceNanos) {
            return;
        }
        String kind = ViewerHealthService.failure(payload);
        if (Objects.equals(kind, current)) {
            return;
        }
        Instant now = clock.instant();
        if (openId != null) {
            jdbc.update("UPDATE system_outage SET ended_at = :now, end_reason = :reason WHERE outage_id = :id",
                    new MapSqlParameterSource().addValue("now", utc(now)).addValue("id", openId)
                            .addValue("reason", kind == null ? "RECOVERED" : "CHANGED"));
            log.info("장애 끝: {} (outage_id={})", current, openId);
            openId = null;
        }
        current = kind;
        if (kind == null) {
            return;
        }
        Instant lastSeen = lastSeen(kind, payload);
        openId = jdbc.queryForObject("""
                INSERT INTO system_outage (kind, last_seen_at, detected_at, details)
                VALUES (:kind, :lastSeen, :now, CAST(:details AS jsonb)) RETURNING outage_id
                """, new MapSqlParameterSource().addValue("kind", kind).addValue("lastSeen", lastSeen == null ? null : utc(lastSeen))
                .addValue("now", utc(now)).addValue("details", details(payload)), Long.class);
        log.warn("장애 시작: {} (outage_id={}, 마지막 정상 근거 {}, 감지 {})", kind, openId, lastSeen, now);
    }

    /** 실패한 쪽이 마지막으로 정상이던 근거 시각. 모르면 null */
    private Instant lastSeen(String kind, JsonNode payload) {
        if ("DEVICE_DOWN".equals(kind)) {
            JsonNode last = payload.path("laptop").path("last_heartbeat_at");
            return last.isIntegralNumber() ? Instant.ofEpochMilli(last.longValue()) : null;
        }
        boolean tier0Down = ViewerHealthService.DISCONNECTED.equals(payload.path("tier0_link").asString(null));
        if (tier0Down) {
            HealthState.Received tier0 = state.tier0Health();
            return tier0 == null ? null : tier0.receivedAt();
        }
        if ("CONTROL_HALTED".equals(kind)) {
            // 연결은 살아 있다: 처리 정체면 마지막으로 처리가 나아간 때, 노트북이 관제 정지를 알렸으면 그때와 가까운 처리 진행 시각
            HealthState.Progress progress = state.progress();
            return progress == null ? null : progress.at();
        }
        return null;
    }

    private static String details(JsonNode payload) {
        return "{\"remote_state\":\"" + payload.path("remote_state").asString("") + "\",\"tier0_link\":\""
                + payload.path("tier0_link").asString("") + "\",\"laptop_link\":\"" + payload.path("laptop").path("link").asString("")
                + "\",\"system_health\":\"" + payload.path("system_health").asString("") + "\"}";
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }
}
