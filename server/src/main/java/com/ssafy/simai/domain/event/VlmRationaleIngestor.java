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
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * I1 이벤트 프레임 → LIVE CRITICAL 병합 이벤트당 I5 작업 하나(명세 5.4·12.5).
 * <p>
 * 같은 사건 묶음 상한(S15P31A101-723): 같은 Tier 0·같은 구역(R2·R3 처럼 구역이 없으면 '구역 없음'끼리)에서 같은 사람(world_id 겹침)이거나
 * 같은 규칙인 이벤트의 근거 작업이 관측 시각 {@link #SAME_INCIDENT_WINDOW_MS} 안에 이미 있으면(대기·대여·성공) 새로 요청하지 않는다.
 * 17차에서 ID 가 갈라져 한 사람의 R2 가 4분에 27번 새 이벤트로 열리며 근거 작업도 27건 나갔다. 근거는 설명뿐이라(AI 는 제안만, AGENTS 4)
 * 요청하지 않아도 등급·경보·이벤트 기록은 그대로다. 앞 작업이 실패·시간 초과면 다음 이벤트가 다시 요청한다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class VlmRationaleIngestor implements I1MessageIngestor {

    private static final Logger log = LoggerFactory.getLogger(VlmRationaleIngestor.class);

    /** 같은 사건으로 보고 근거를 한 번만 요청하는 관측 시각 창(ms). 명세에 없는 값이라 설계 문서(-723)에 근거를 둔다 */
    static final long SAME_INCIDENT_WINDOW_MS = 30_000;

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
                    u.observation_at, u.evidence_ref, u.calibration_version
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
            input.put("calibration_version", rs.getString("calibration_version"));
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
        if (sameIncidentJob(id, envelope.sourceId(), input.path("observation_time").longValue()) != null) {
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
        input.remove("calibration_version");
        jobs.enqueue(new JobSpec("vlm_rationale", input, 2, 5000, id, null));
        return Outcome.STORED;
    }

    /**
     * 같은 사건으로 이미 요청한 근거 작업의 이벤트 ID(없으면 null). 같은 Tier 0 의 판단을 한 줄로 세우려고 트랜잭션 범위 권고 잠금을 먼저 잡는다
     * (서로 다른 이벤트의 통지가 동시에 와도 하나만 요청한다).
     */
    private String sameIncidentJob(UUID id, String gateway, long observationTime) {
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("id", id)
                .addValue("gateway", gateway).addValue("since", observationTime - SAME_INCIDENT_WINDOW_MS)
                .addValue("until", observationTime + SAME_INCIDENT_WINDOW_MS);
        jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.vlm_rationale:' || :gateway))", params);
        List<String> prior = jdbc.queryForList("""
                SELECT p.event_id::text FROM analysis_job j
                JOIN risk_event p ON p.event_id = j.event_id
                JOIN risk_event cur ON cur.event_id = :id
                WHERE j.job_type = 'vlm_rationale' AND j.status IN ('QUEUED', 'LEASED', 'SUCCEEDED')
                  AND p.event_id <> cur.event_id AND p.source = 'LIVE' AND p.gateway_id = cur.gateway_id
                  AND p.zone_id IS NOT DISTINCT FROM cur.zone_id
                  AND (p.rule_id = cur.rule_id OR p.world_ids && cur.world_ids)
                  AND (j.input ->> 'observation_time')::bigint BETWEEN :since AND :until
                ORDER BY j.created_at DESC LIMIT 1
                """, params, String.class);
        if (prior.isEmpty()) {
            return null;
        }
        log.info("VLM 근거 요청 생략(같은 사건 {} ms 창): event={} 앞선 근거 event={}", SAME_INCIDENT_WINDOW_MS, id, prior.getFirst());
        return prior.getFirst();
    }

    private ObjectNode structuredState(ObjectNode input, String gateway) {
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("at", I1Json.utc(Instant.ofEpochMilli(input.path("observation_time").longValue())))
                .addValue("gateway", gateway).addValue("ref", input.path("world_state_ref").asString(null));
        ObjectNode state = mapper.createObjectNode();
        state.set("calibration_version", input.get("calibration_version"));
        var tracks = state.putArray("tracks");
        for (JsonNode id : input.get("world_ids")) {
            params.addValue("world", id.asString());
            jdbc.query("""
                    SELECT world_id, ST_X(position) AS x, ST_Y(position) AS y, head_z, head_z_validity,
                        array_to_json(ground_anchor_xyz)::text AS ground_anchor_xyz,
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
                if (rs.getString("ground_anchor_xyz") != null) {
                    track.set("ground_anchor_xyz", mapper.readTree(rs.getString("ground_anchor_xyz")));
                }
                track.put("head_z_validity", rs.getString("head_z_validity"));
                track.put("observation_state", rs.getString("observation_state"));
                if (rs.getString("input_provenance") != null) {
                    track.set("input_provenance", mapper.readTree(rs.getString("input_provenance")));
                }
            });
        }
        ObjectNode zone = state.putObject("zone").put("zone_id", input.path("zone_id").asString(null));
        zone.putNull("judge_type");
        params.addValue("zone", input.path("zone_id").asString(null));
        jdbc.query("""
                SELECT eval_type, ST_AsGeoJSON(polygon)::text AS polygon, z_min_m
                FROM zone WHERE zone_id = :zone AND valid_from <= :at
                    AND (valid_to IS NULL OR valid_to > :at) ORDER BY zone_version DESC LIMIT 1
                """, params, rs -> {
            zone.put("judge_type", rs.getString("eval_type"));
            zone.set("polygon_xy", mapper.readTree(rs.getString("polygon")).path("coordinates").get(0));
            if ("GROUND_FOOTPRINT".equals(rs.getString("eval_type"))) {
                zone.put("height_m", 0.0);
            } else if (rs.getObject("z_min_m") != null) {
                zone.put("height_m", rs.getDouble("z_min_m"));
            }
        });
        return state;
    }
}
