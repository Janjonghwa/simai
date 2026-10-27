package com.ssafy.simai.domain.clip;

import com.ssafy.simai.domain.clip.ClipDeferral.Deferrable;
import com.ssafy.simai.domain.clip.ClipDeferral.WaitingForEarlier;
import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * media_clip_retention(Tier 0 → EC2, I1, 명세 11.5 CRITICAL 자동 보존) → 보존 이력 한 행 + 클립의 현재 보존 표시를 켠다.
 * HIGH 로 시작한 위험 구간이 클립을 보낸 뒤 CRITICAL 로 오를 때 온다. Tier 0 는 붙이기만 한다(떼기는 관리자, ClipService).
 * 클립을 보낸 Tier 0 의 기록만 받는다. 같은 msg_id 는 한 행(V11). 클립 기록보다 먼저 오면 보관했다가 다시 처리한다(ClipDeferral).
 * 이미 지운 클립(보관 기간 지남)은 되살릴 수 없으므로 보존 표시를 켜지 않는다(관리자 경로의 410 과 같다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class MediaClipRetentionIngestor implements Deferrable {

    private static final Logger log = LoggerFactory.getLogger(MediaClipRetentionIngestor.class);
    private static final String SCHEMA = "messages/media_clip_retention";

    private final NamedParameterJdbcTemplate jdbc;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;
    private final ClipDeferral deferral;

    MediaClipRetentionIngestor(NamedParameterJdbcTemplate jdbc, ContractSchemas schemas, ObjectMapper mapper,
            TransactionTemplate transaction, ClipDeferral deferral) {
        this.jdbc = jdbc;
        this.schemas = schemas;
        this.mapper = mapper;
        this.transaction = transaction;
        this.deferral = deferral;
    }

    @Override
    public String msgType() {
        return "media_clip_retention";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1);
    }

    @Override
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        return deferral.applyOrPark(this, envelope, receivedAt);
    }

    @Override
    public Outcome apply(I1Envelope envelope, Instant receivedAt) {
        List<String> problems = schemas.violations(SCHEMA, mapper.writeValueAsString(envelope.toContractJson(mapper)));
        if (!problems.isEmpty()) {
            throw new InvalidI1Message("media_clip_retention 이 계약에 맞지 않는다: " + problems);
        }
        JsonNode p = envelope.payload();
        if (!envelope.sourceId().equals(p.get("gateway_id").asString())) {
            throw new InvalidI1Message("gateway_id 가 source_id 와 다르다");
        }
        UUID clipId = I1Json.requiredUuid(p, "clip_id");
        UUID eventId = I1Json.requiredUuid(p, "event_id");
        String gatewayId = p.get("gateway_id").asString();
        int resumeCount = p.get("resume_count").asInt();
        MapSqlParameterSource params = new MapSqlParameterSource("clipId", clipId);
        return transaction.execute(status -> {
            List<Map<String, Object>> clips = jdbc.queryForList("""
                    SELECT event_id, resume_count, gateway_id, deleted_at FROM media_clip WHERE clip_id = :clipId FOR UPDATE
                    """, params);
            if (clips.isEmpty()) {
                throw new WaitingForEarlier("클립 기록을 아직 받지 못했다");
            }
            Map<String, Object> clip = clips.getFirst();
            // 클립을 보낸 Tier 0 만 그 클립에 보존 표시를 붙인다
            if (!clip.get("event_id").equals(eventId) || ((Number) clip.get("resume_count")).intValue() != resumeCount
                    || !clip.get("gateway_id").equals(gatewayId)) {
                throw new InvalidI1Message("media_clip_retention 의 이벤트·위험 구간·Tier 0 가 클립과 다르다");
            }
            if (clip.get("deleted_at") != null) {
                log.info("이미 지운 클립의 보존 기록이라 반영하지 않음: clip_id={}", clipId);
                return Outcome.DUPLICATE;
            }
            if (!ClipRetentions.recordTier0(jdbc, clipId, eventId, gatewayId, resumeCount, p.get("severity").asString(),
                    p.get("reason").asString(), I1Json.requiredTime(p, "issued_at_ms"), envelope.msgId())) {
                return Outcome.DUPLICATE;
            }
            jdbc.update("UPDATE media_clip SET retention_hold = true WHERE clip_id = :clipId", params);
            return Outcome.STORED;
        });
    }
}
