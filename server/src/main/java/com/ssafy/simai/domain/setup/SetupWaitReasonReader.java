package com.ssafy.simai.domain.setup;

import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** {@link SetupWaitReason} 의 판단 재료를 DB 에서 읽는다(읽기만, S15P31A101-583). */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupWaitReasonReader {

    private static final Duration HEALTH_REPORTED_SLACK = Duration.ofMinutes(10);

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    SetupWaitReasonReader(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 세션 단계·대여·수집률(세션 행 값)과 작업 ID 로 재료를 모은다 */
    SetupWaitReason.Facts read(String sessionStatus, UUID progressLeaseId, String backgroundCollectionJson, String jobId,
            Instant now) {
        Map<String, Double> bg = backgroundPct(backgroundCollectionJson);
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("job", jobId)
                .addValue("now", utc(now))
                .addValue("healthFrom", utc(now.minus(SetupWaitReason.TIER0_HEALTH_WINDOW)))
                // system_health 는 reported_at(Tier 0 시계)으로 나뉜 hypertable 이다. 시계 차이를 넉넉히 두고 오래된 조각을 거른다
                .addValue("reportedFrom", utc(now.minus(HEALTH_REPORTED_SLACK)));
        List<Map<String, Object>> jobs = jobId == null ? List.of() : jdbc.queryForList("""
                SELECT j.status, j.created_at, j.error ->> 'code' AS error_code, j.error ->> 'message' AS error_message,
                       (SELECT max(GREATEST(l.leased_at, l.last_heartbeat_at, l.submitted_at)) FROM job_lease l
                        WHERE l.job_id = j.job_id) AS last_lease_activity_at,
                       a.lease_id AS active_lease_id, a.leased_at AS active_leased_at
                FROM analysis_job j
                LEFT JOIN LATERAL (
                    SELECT lease_id, leased_at FROM job_lease
                    WHERE job_id = j.job_id AND status = 'ACTIVE' AND lease_expires_at > :now
                    ORDER BY leased_at DESC LIMIT 1) a ON true
                WHERE j.job_id = :job
                """, params);
        if (jobs.isEmpty()) {
            return new SetupWaitReason.Facts(sessionStatus, progressLeaseId, bg, null, null, null, null, null, null, null,
                    0, null, null, 0, null, false);
        }
        Map<String, Object> job = jobs.getFirst();
        Map<String, Object> workers = jdbc.queryForMap("""
                SELECT count(*) AS eligible, max(w.last_seen_at) AS last_seen_at,
                       (SELECT j2.job_type FROM job_lease l JOIN analysis_job j2 ON j2.job_id = l.job_id
                        WHERE l.status = 'ACTIVE' AND l.lease_expires_at > :now AND l.job_id <> :job
                          AND l.worker_id IN (SELECT worker_id FROM worker
                                              WHERE revoked_at IS NULL AND 'site_setup' = ANY(job_types))
                        ORDER BY l.leased_at DESC LIMIT 1) AS other_job_type
                FROM worker w
                WHERE w.revoked_at IS NULL AND 'site_setup' = ANY(w.job_types)
                """, params);
        Map<String, Object> health = jdbc.queryForMap("""
                SELECT count(*) AS samples, max(pipeline_lag_ms) AS lag_max,
                       bool_or(pipeline_lag_ms IS NULL) AS lag_unknown
                FROM system_health
                WHERE reported_at >= :reportedFrom AND received_at >= :healthFrom AND received_at <= :now
                """, params);
        return new SetupWaitReason.Facts(
                sessionStatus,
                progressLeaseId,
                bg,
                (String) job.get("status"),
                instant(job.get("created_at")),
                (String) job.get("error_code"),
                (String) job.get("error_message"),
                instant(job.get("last_lease_activity_at")),
                (UUID) job.get("active_lease_id"),
                instant(job.get("active_leased_at")),
                ((Number) workers.get("eligible")).intValue(),
                instant(workers.get("last_seen_at")),
                (String) workers.get("other_job_type"),
                ((Number) health.get("samples")).intValue(),
                health.get("lag_max") == null ? null : ((Number) health.get("lag_max")).intValue(),
                Boolean.TRUE.equals(health.get("lag_unknown")));
    }

    private Map<String, Double> backgroundPct(String json) {
        Map<String, Double> out = new LinkedHashMap<>();
        if (json == null) {
            return out;
        }
        JsonNode node = mapper.readTree(json);
        if (node != null && node.isObject()) {
            for (Map.Entry<String, JsonNode> e : node.properties()) {
                if (e.getValue().isNumber()) {
                    out.put(e.getKey(), e.getValue().doubleValue());
                }
            }
        }
        return out;
    }

    /** timestamptz 열 값(드라이버에 따라 Timestamp·OffsetDateTime) → Instant, 없으면 null */
    private static Instant instant(Object value) {
        if (value instanceof Timestamp timestamp) {
            return timestamp.toInstant();
        }
        return value instanceof OffsetDateTime time ? time.toInstant() : null;
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }
}
