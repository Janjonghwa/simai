package com.ssafy.simai.domain.health;

import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Objects;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 장애(관제 정지·현장 장비 응답 없음·원격 전달 단절, FR-11)가 시작·바뀜·끝날 때 system_outage 에 남기고 로그를 쓴다(명세 6.5, G3).
 * 화면 알림은 viewer_health(/topic/health, 바뀌면 즉시)가 하고, 이 기록은 "단절 10초 안에 관리자 화면 알림"(G3)을 나중에 재는 근거다:
 * last_seen_at(실패한 쪽의 마지막 정상 근거) → detected_at(EC2 가 장애로 본 시각).
 * HealthPublisher 의 전용 스레드에서만 부른다. 기록에 실패하면 상태를 바꾸지 않아 다음 주기(1초)에 다시 시도한다.
 * EC2 가 뜬 뒤(ApplicationReady) Tier 0·노트북이 다시 붙을 시간(outage-startup-grace-ms)을 준다: 둘 다 한 번씩 받았거나 그 시간이 지나야 기록한다
 * (재시작·재배포를 현장 장애로 남기지 않는다). 뜰 때 닫히지 않은 기록은 EC2_RESTART 로 닫는다. EC2 한 대 기준이다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class OutageRecorder {

    private static final Logger log = LoggerFactory.getLogger(OutageRecorder.class);

    private final NamedParameterJdbcTemplate jdbc;
    private final HealthState state;
    private final Clock clock;
    private final ObjectMapper mapper;
    private final long linkTimeoutNanos;
    /** 뜬 뒤 Tier 0·노트북 재연결을 기다리는 시간(simai.health.outage-startup-grace-ms, 기본 30초) */
    private final long startupGraceNanos;
    private volatile long readyNanos;
    private volatile boolean ready;
    private String current;
    private Long openId;

    OutageRecorder(NamedParameterJdbcTemplate jdbc, HealthState state, ViewerHealthService health, Clock clock,
            ObjectMapper mapper, @Value("${simai.health.outage-startup-grace-ms:30000}") long startupGraceMs) {
        this.startupGraceNanos = startupGraceMs * 1_000_000L;
        this.jdbc = jdbc;
        this.state = state;
        this.clock = clock;
        this.mapper = mapper;
        this.linkTimeoutNanos = health.linkTimeoutMs() * 1_000_000L;
        int closed = jdbc.update("UPDATE system_outage SET ended_at = :now, end_reason = 'EC2_RESTART' WHERE ended_at IS NULL",
                new MapSqlParameterSource("now", utc(clock.instant())));
        if (closed > 0) {
            log.info("EC2 재시작 전 열린 장애 기록을 닫음: {}건", closed);
        }
    }

    @EventListener(ApplicationReadyEvent.class)
    void onReady() {
        readyNanos = state.nanoTime();
        ready = true;
    }

    void observe(JsonNode payload) {
        if (!ready) {
            return;
        }
        boolean bothSeen = state.tier0Health() != null && !state.laptops().isEmpty();
        if (!bothSeen && state.nanoTime() - readyNanos < startupGraceNanos) {
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
        if (kind != null) {
            Instant lastSeen = lastSeen(kind, payload);
            openId = jdbc.queryForObject("""
                    INSERT INTO system_outage (kind, last_seen_at, detected_at, details)
                    VALUES (:kind, :lastSeen, :now, CAST(:details AS jsonb)) RETURNING outage_id
                    """, new MapSqlParameterSource().addValue("kind", kind).addValue("lastSeen", lastSeen == null ? null : utc(lastSeen))
                    .addValue("now", utc(now)).addValue("details", details(payload)), Long.class);
            log.warn("장애 시작: {} (outage_id={}, 마지막 정상 근거 {}, 감지 {})", kind, openId, lastSeen, now);
        }
        // 기록이 끝난 뒤에만 바꾼다. 실패하면(예외) 다음 주기에 다시 시도한다
        current = kind;
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
            // 연결은 살아 있다. 처리가 멈췄으면 마지막으로 나아간 때, 처리는 도는데 노트북이 관제 정지를 알렸으면 노트북이 마지막으로 정상을 알린 때
            HealthState.Progress progress = state.progress();
            boolean processing = progress != null && state.nanoTime() - progress.nanos() <= linkTimeoutNanos;
            return processing ? state.laptopOkAt() : progress == null ? null : progress.at();
        }
        return null;
    }

    private String details(JsonNode payload) {
        ObjectNode d = mapper.createObjectNode();
        d.put("remote_state", payload.path("remote_state").asString(""));
        d.put("tier0_link", payload.path("tier0_link").asString(""));
        d.put("laptop_link", payload.path("laptop").path("link").asString(""));
        d.put("system_health", payload.path("system_health").asString(""));
        return mapper.writeValueAsString(d);
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }
}
