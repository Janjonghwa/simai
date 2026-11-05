package com.ssafy.simai.domain.ppe;

import static com.ssafy.simai.domain.tier0.i1.I1Json.utc;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * ppe_state(PPE 프로세스 → Tier 0 I1 → EC2, 1~2 Hz, S15P31A101-516, 명세 5.5·12.3·11장).
 * 계약 스키마 전체와 스키마가 못 거는 계약 시험(PpeStateChecks)으로 검사한 뒤, people 항목마다 ppe_state 에 한 행을 추가만 하고
 * 사람별 현재 상태(ppe_current_state)는 더 새 메시지일 때만 덮는다. 멱등 키는 (source_id, stream_epoch, state_sequence) 다.
 * 관제 화면 전달(I2 토픽)은 명세 19.1 미결이라 여기서 하지 않는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class PpeStateIngestor implements I1MessageIngestor {

    private static final String SCHEMA = "messages/ppe_state";
    /**
     * 촬영 시각이 수신 시각보다 이만큼 넘게 지난 상태는 이력에만 두고 현재 상태를 덮지 않는다.
     * 계약 expires_at_ms 초기값(마지막 근거 + 10초, 5.5)과 같은 값이다. 그보다 늦게 온 상태는 받는 순간 이미 만료된 근거다.
     */
    static final Duration STALE_AFTER = Duration.ofSeconds(10);
    /** 재전송을 찾을 때 볼 촬영 시각 범위(재연결 재전송은 분 단위라 하루면 충분하다. 압축 청크까지 훑지 않게 범위를 건다) */
    private static final Duration DUPLICATE_WINDOW = Duration.ofDays(1);

    private static final String INSERT = """
            INSERT INTO ppe_state (observation_at, source_id, stream_epoch, state_sequence, world_id, msg_id, identity_revision,
                helmet_state, helmet_reason, helmet_valid_cells, helmet_support_ratio, helmet_last_evidence_at, helmet_expires_at,
                helmet_evidence, vest_state, vest_reason, vest_valid_cells, vest_support_ratio, vest_last_evidence_at, vest_expires_at,
                vest_evidence, model_version, checkpoint_sha256, policy_version, input_mode, input_provenance, received_at)
            VALUES (:observationAt, :sourceId, :streamEpoch, :stateSequence, :worldId, :msgId, :identityRevision,
                :helmetState, :helmetReason, :helmetValidCells, :helmetSupportRatio, :helmetLastEvidenceAt, :helmetExpiresAt,
                CAST(:helmetEvidence AS jsonb), :vestState, :vestReason, :vestValidCells, :vestSupportRatio, :vestLastEvidenceAt,
                :vestExpiresAt, CAST(:vestEvidence AS jsonb), :modelVersion, :checkpointSha256, :policyVersion, :inputMode,
                CAST(:inputProvenance AS jsonb), :receivedAt)
            ON CONFLICT DO NOTHING
            """;

    /** (identity_revision, 촬영 시각, stream_epoch, state_sequence) 가 더 큰 상태만 덮는다. 과거·순서 뒤바뀐 메시지는 바꾸지 않는다 */
    private static final String UPSERT_CURRENT = """
            INSERT INTO ppe_current_state (source_id, world_id, identity_revision, observation_at, stream_epoch, state_sequence,
                helmet_state, helmet_reason, helmet_last_evidence_at, helmet_expires_at, vest_state, vest_reason,
                vest_last_evidence_at, vest_expires_at, model_version, policy_version, updated_at)
            VALUES (:sourceId, :worldId, :identityRevision, :observationAt, :streamEpoch, :stateSequence,
                :helmetState, :helmetReason, :helmetLastEvidenceAt, :helmetExpiresAt, :vestState, :vestReason,
                :vestLastEvidenceAt, :vestExpiresAt, :modelVersion, :policyVersion, :receivedAt)
            ON CONFLICT (source_id, world_id) DO UPDATE SET
                identity_revision = EXCLUDED.identity_revision, observation_at = EXCLUDED.observation_at,
                stream_epoch = EXCLUDED.stream_epoch, state_sequence = EXCLUDED.state_sequence,
                helmet_state = EXCLUDED.helmet_state, helmet_reason = EXCLUDED.helmet_reason,
                helmet_last_evidence_at = EXCLUDED.helmet_last_evidence_at, helmet_expires_at = EXCLUDED.helmet_expires_at,
                vest_state = EXCLUDED.vest_state, vest_reason = EXCLUDED.vest_reason,
                vest_last_evidence_at = EXCLUDED.vest_last_evidence_at, vest_expires_at = EXCLUDED.vest_expires_at,
                model_version = EXCLUDED.model_version, policy_version = EXCLUDED.policy_version, updated_at = EXCLUDED.updated_at
            WHERE (EXCLUDED.identity_revision, EXCLUDED.observation_at, EXCLUDED.stream_epoch, EXCLUDED.state_sequence)
                > (ppe_current_state.identity_revision, ppe_current_state.observation_at, ppe_current_state.stream_epoch,
                   ppe_current_state.state_sequence)
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;

    PpeStateIngestor(NamedParameterJdbcTemplate jdbc, ContractSchemas schemas, ObjectMapper mapper, TransactionTemplate transaction) {
        this.jdbc = jdbc;
        this.schemas = schemas;
        this.mapper = mapper;
        this.transaction = transaction;
    }

    @Override
    public String msgType() {
        return "ppe_state";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1);
    }

    /**
     * people 가 빈 메시지(판정할 사람 없음, 프로세스는 살아 있음)는 쓸 행이 없어 STORED 로 센다. 재전송인지 가릴 행도 없지만
     * 다시 받아도 바뀌는 것이 없다.
     */
    @Override
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        List<String> problems = schemas.violations(SCHEMA, mapper.writeValueAsString(envelope.toContractJson(mapper)));
        if (!problems.isEmpty()) {
            // 검사 문구는 경로와 규칙만 담는다(값 없음)
            throw new InvalidI1Message("ppe_state 가 계약에 맞지 않는다: " + problems);
        }
        JsonNode p = envelope.payload();
        PpeStateChecks.check(p);
        JsonNode people = p.get("people");
        if (people.isEmpty()) {
            return Outcome.STORED;
        }
        long streamEpoch = p.get("stream_epoch").asLong();
        long stateSequence = p.get("state_sequence").asLong();
        MapSqlParameterSource key = new MapSqlParameterSource()
                .addValue("sourceId", envelope.sourceId())
                .addValue("streamEpoch", streamEpoch)
                .addValue("stateSequence", stateSequence)
                .addValue("since", utc(earliestCapture(people).minus(DUPLICATE_WINDOW)));
        return transaction.execute(status -> {
            // 아직 행이 없는 메시지는 잠기지 않는다. 같은 멱등 키를 잠가 재연결로 겹쳐 온 같은 메시지를 한 번만 쓴다
            jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.ppe:' || :sourceId || ':' || :streamEpoch || ':'"
                    + " || :stateSequence))", key);
            boolean seen = !jdbc.queryForList("""
                    SELECT 1 FROM ppe_state WHERE source_id = :sourceId AND stream_epoch = :streamEpoch
                        AND state_sequence = :stateSequence AND observation_at >= :since LIMIT 1
                    """, key).isEmpty();
            if (seen) {
                return Outcome.DUPLICATE;
            }
            for (JsonNode person : people) {
                MapSqlParameterSource row = row(envelope, p, person, receivedAt);
                jdbc.update(INSERT, row);
                if (!isStale(person, receivedAt)) {
                    jdbc.update(UPSERT_CURRENT, row);
                }
            }
            return Outcome.STORED;
        });
    }

    /** 촬영 시각 + STALE_AFTER 가 수신 시각보다 이르면 받는 순간 이미 만료된 상태다(Tier 0 시계와 EC2 시계를 같은 UTC 로 본다) */
    static boolean isStale(JsonNode person, Instant receivedAt) {
        Instant capture = Instant.ofEpochMilli(person.get("capture_time_ms").asLong());
        return capture.plus(STALE_AFTER).isBefore(receivedAt);
    }

    private static Instant earliestCapture(JsonNode people) {
        long min = Long.MAX_VALUE;
        for (JsonNode person : people) {
            min = Math.min(min, person.get("capture_time_ms").asLong());
        }
        return Instant.ofEpochMilli(min);
    }

    private MapSqlParameterSource row(I1Envelope envelope, JsonNode p, JsonNode person, Instant receivedAt) {
        MapSqlParameterSource row = new MapSqlParameterSource()
                .addValue("observationAt", I1Json.requiredTime(person, "capture_time_ms"))
                .addValue("sourceId", envelope.sourceId())
                .addValue("streamEpoch", p.get("stream_epoch").asLong())
                .addValue("stateSequence", p.get("state_sequence").asLong())
                .addValue("worldId", person.get("world_id").asString())
                .addValue("msgId", envelope.msgId())
                .addValue("identityRevision", person.get("identity_revision").asInt())
                .addValue("modelVersion", p.get("model_version").asString())
                .addValue("checkpointSha256", p.get("checkpoint_sha256").asString())
                .addValue("policyVersion", p.get("policy_version").asString())
                .addValue("inputMode", p.get("input_mode").asString())
                .addValue("inputProvenance", mapper.writeValueAsString(p.get("input_provenance")))
                .addValue("receivedAt", utc(receivedAt));
        item(row, "helmet", person.get("helmet"));
        item(row, "vest", person.get("vest"));
        return row;
    }

    private void item(MapSqlParameterSource row, String name, JsonNode item) {
        row.addValue(name + "State", item.get("state").asString())
                .addValue(name + "Reason", item.get("reason").asString())
                .addValue(name + "ValidCells", item.get("valid_cells").asInt())
                .addValue(name + "SupportRatio", item.get("support_ratio").asDouble())
                .addValue(name + "LastEvidenceAt", I1Json.time(item, "last_evidence_at_ms"))
                .addValue(name + "ExpiresAt", I1Json.time(item, "expires_at_ms"))
                .addValue(name + "Evidence", mapper.writeValueAsString(item.get("evidence")));
    }
}
