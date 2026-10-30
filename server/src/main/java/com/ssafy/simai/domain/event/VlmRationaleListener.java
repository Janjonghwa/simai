package com.ssafy.simai.domain.event;

import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.domain.tier0.event.RiskEventsDelivered;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.context.event.EventListener;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** 제출 트랜잭션 안에서 근거를 추가하고 최신 근거 투영을 갱신한다. 커밋 뒤 기존 I2 릴레이가 발행한다. */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class VlmRationaleListener {

    private static final Logger log = LoggerFactory.getLogger(VlmRationaleListener.class);
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final ApplicationEventPublisher publisher;

    public VlmRationaleListener(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, Clock clock,
            ApplicationEventPublisher publisher) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.clock = clock;
        this.publisher = publisher;
    }

    @EventListener
    public void finished(JobFinished event) {
        if (!"vlm_rationale".equals(event.jobType()) || !"SUCCEEDED".equals(event.status())) {
            return;
        }
        MapSqlParameterSource params = new MapSqlParameterSource("job", event.jobId());
        List<JsonNode> inputs = jdbc.queryForList("""
                SELECT input::text FROM analysis_job WHERE job_id = :job AND event_id IS NOT NULL
                """, params, String.class).stream().map(mapper::readTree).toList();
        // 이벤트에 연결하지 않은 관리·시험 작업은 빠른 경로 투영 대상이 아니다.
        if (inputs.isEmpty()) {
            return;
        }
        JsonNode input = inputs.getFirst();
        JsonNode result = event.result();
        if (!matchesInput(input, result)) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        UUID id = UUID.fromString(input.path("event_id").asString());
        params.addValue("id", id);
        List<String> severities = jdbc.queryForList("""
                SELECT severity FROM risk_event WHERE event_id = :id AND source = 'LIVE' FOR UPDATE
                """, params, String.class);
        if (severities.isEmpty() || !severities.getFirst().equals(result.path("severity").asString())) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        // 저장 시각은 ms 로 자른다. 리눅스 시계는 µs 까지 주는데 created_at(µs 보존)과 event_to_rationale_ms(ms 내림)가 어긋나지 않게 한다
        Instant savedAt = clock.instant().truncatedTo(ChronoUnit.MILLIS);
        long observed = input.path("observation_time").longValue();
        long elapsed = savedAt.toEpochMilli() - observed;
        params.addValue("sequence", result.path("event_sequence").intValue())
                .addValue("model", result.path("model_version").asString()).addValue("input", input.toString())
                .addValue("severity", result.path("severity").asString()).addValue("text", result.path("rationale").asString())
                .addValue("latency", result.has("latency_ms") ? result.path("latency_ms").intValue() : null)
                .addValue("observed", I1Json.utc(Instant.ofEpochMilli(observed))).addValue("saved", I1Json.utc(savedAt))
                .addValue("elapsed", elapsed);
        int inserted = jdbc.update("""
                INSERT INTO vlm_assessment (event_id, event_sequence, job_id, job_type, model_version, input_ref,
                    rule_severity, text, clause_ids, proposed_severity, latency_ms, decision,
                    event_observed_at, created_at, event_to_rationale_ms)
                VALUES (:id, :sequence, :job, 'vlm_rationale', :model, CAST(:input AS jsonb), :severity, :text,
                    '{}'::text[], NULL, :latency, 'NONE', :observed, :saved, :elapsed)
                ON CONFLICT (job_id) DO NOTHING
                """, params);
        if (inserted == 0) {
            return;
        }
        // 등급·생명주기·sequence·위치에는 쓰지 않는다. 원본 전이도 그대로 둔다.
        jdbc.update("UPDATE risk_event SET rationale = :text, proposed_severity = NULL WHERE event_id = :id", params);
        log.info("VLM 근거 저장: event_id={} job_id={} event_to_rationale_ms={}", id, event.jobId(), elapsed);
        publisher.publishEvent(new RiskEventsDelivered(List.of(id)));
    }

    static boolean matchesInput(JsonNode input, JsonNode result) {
        if (result == null || !result.isObject()) {
            return false;
        }
        for (String field : List.of("event_id", "event_sequence", "severity")) {
            if (!input.path(field).equals(result.path(field))) {
                return false;
            }
        }
        return result.has("proposed_severity") && result.path("proposed_severity").isNull()
                && (!result.has("clause_ids") || (result.path("clause_ids").isArray() && result.path("clause_ids").isEmpty()));
    }
}
