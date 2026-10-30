package com.ssafy.simai.domain.event;

import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** I1 이벤트 프레임 → LIVE CRITICAL 병합 이벤트당 I5 작업 하나(명세 5.4·12.5). */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class VlmRationaleIngestor implements I1MessageIngestor {

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final ContractSchemas schemas;
    private final AnalysisJobService jobs;

    public VlmRationaleIngestor(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, ContractSchemas schemas,
            AnalysisJobService jobs) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.schemas = schemas;
        this.jobs = jobs;
    }

    @Override
    public String msgType() {
        return "event_frame_ready";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1);
    }

    @Override
    @Transactional
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        if (!schemas.violations("messages/event_frame_ready", envelope.toContractJson(mapper).toString()).isEmpty()) {
            throw new InvalidI1Message("event_frame_ready 가 계약에 맞지 않는다");
        }
        UUID id = I1Json.requiredUuid(envelope.payload(), "event_id");
        MapSqlParameterSource params = new MapSqlParameterSource("id", id);
        // 이벤트 잠금 안에서 검사·등록한다. 다른 msg_id/연결의 동시 통지도 한 작업으로 모인다.
        List<ObjectNode> found = jdbc.query("""
                SELECT e.source, e.gateway_id, e.severity, e.delivery, e.event_sequence, e.rule_id, e.rule_version,
                    e.confidence, e.needs_check, to_json(e.world_ids)::text AS world_ids, e.zone_id, e.metrics::text AS metrics,
                    u.observation_at, u.evidence_ref
                FROM risk_event e LEFT JOIN risk_event_update u
                    ON u.event_id = e.event_id AND u.event_sequence = e.event_sequence
                WHERE e.event_id = :id FOR UPDATE OF e
                """, params, (rs, i) -> {
            ObjectNode input = mapper.createObjectNode();
            for (String field : List.of("source", "gateway_id", "severity", "delivery", "rule_id", "confidence", "zone_id")) {
                input.put(field, rs.getString(field));
            }
            input.put("event_id", id.toString());
            input.put("event_sequence", rs.getInt("event_sequence"));
            input.put("rule_version", rs.getInt("rule_version"));
            input.put("needs_check", rs.getBoolean("needs_check"));
            input.set("world_ids", mapper.readTree(rs.getString("world_ids")));
            input.set("metrics", mapper.readTree(rs.getString("metrics")));
            if (rs.getTimestamp("observation_at") != null) {
                input.put("observation_time", rs.getTimestamp("observation_at").getTime());
            }
            input.put("world_state_ref", rs.getString("evidence_ref"));
            return input;
        });
        if (found.isEmpty()) {
            return Outcome.DUPLICATE;
        }
        ObjectNode input = found.getFirst();
        if (!envelope.sourceId().equals(input.path("gateway_id").asString())) {
            throw new InvalidI1Message("event_frame_ready 의 source_id 가 이벤트 gateway_id 와 다르다");
        }
        if (!"LIVE".equals(input.path("source").asString()) || !"CRITICAL".equals(input.path("severity").asString())
                || "AUDIT_ONLY".equals(input.path("delivery").asString()) || !input.has("observation_time")
                || !receivedAt.isBefore(Instant.ofEpochMilli(input.path("observation_time").longValue()).plusMillis(5000))
                || !jdbc.queryForList("SELECT 1 FROM analysis_job WHERE event_id = :id AND job_type = 'vlm_rationale'", params).isEmpty()) {
            return Outcome.DUPLICATE;
        }
        input.remove(List.of("source", "gateway_id", "delivery"));
        if (input.path("world_state_ref").isNull()) {
            input.remove("world_state_ref");
        }
        var frames = input.putArray("frames");
        for (JsonNode frame : envelope.payload().get("frames")) {
            ObjectNode copy = (ObjectNode) frame.deepCopy();
            copy.put("face_policy", "BLURRED");
            frames.add(copy);
        }
        input.set("structured_state", structuredState(input, envelope.sourceId()));
        jobs.enqueue(new JobSpec("vlm_rationale", input, 2, 5000, id, null));
        return Outcome.STORED;
    }

    private ObjectNode structuredState(ObjectNode input, String gateway) {
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("at", I1Json.utc(Instant.ofEpochMilli(input.path("observation_time").longValue())))
                .addValue("gateway", gateway).addValue("ref", input.path("world_state_ref").asString(null));
        ObjectNode state = mapper.createObjectNode();
        var tracks = state.putArray("tracks");
        for (JsonNode id : input.get("world_ids")) {
            params.addValue("world", id.asString());
            jdbc.query("""
                    SELECT world_id, ST_X(position) AS x, ST_Y(position) AS y, head_z, head_z_validity,
                        observation_state, input_provenance::text AS input_provenance
                    FROM world_track_state WHERE gateway_id = :gateway AND world_id = :world
                      AND observation_at <= :at
                      AND ((CAST(:ref AS uuid) IS NOT NULL AND msg_id = CAST(:ref AS uuid))
                           OR (CAST(:ref AS uuid) IS NULL AND observation_at = :at))
                    ORDER BY observation_at DESC LIMIT 1
                    """, params, rs -> {
                ObjectNode track = tracks.addObject().put("world_id", rs.getString("world_id"));
                track.putArray("position_xy").add(rs.getDouble("x")).add(rs.getDouble("y"));
                if ("VALID".equals(rs.getString("head_z_validity")) && rs.getObject("head_z") != null) {
                    track.put("head_z", rs.getDouble("head_z"));
                } else {
                    track.putNull("head_z");
                }
                track.put("head_z_validity", rs.getString("head_z_validity"));
                track.put("observation_state", rs.getString("observation_state"));
                if (rs.getString("input_provenance") != null) {
                    track.set("input_provenance", mapper.readTree(rs.getString("input_provenance")));
                }
            });
        }
        ObjectNode zone = state.putObject("zone").put("zone_id", input.path("zone_id").asString(null));
        params.addValue("zone", input.path("zone_id").asString(null));
        zone.put("judge_type", jdbc.queryForList("""
                SELECT eval_type FROM zone WHERE zone_id = :zone AND valid_from <= :at
                    AND (valid_to IS NULL OR valid_to > :at) ORDER BY zone_version DESC LIMIT 1
                """, params, String.class).stream().findFirst().orElse(null));
        return state;
    }
}
