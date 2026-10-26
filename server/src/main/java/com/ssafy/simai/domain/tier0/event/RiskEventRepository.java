package com.ssafy.simai.domain.tier0.event;

import static com.ssafy.simai.domain.tier0.i1.I1Json.*;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.time.Instant;
import java.util.Collection;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;

/**
 * risk_event(현재 상태)·risk_event_update(Tier 0 전이 기록, 추가만)를 쓴다(V1, docs/design/db-tables.md E절, 명세 3.2).
 * EC2 는 전이를 만들지 않는다. 받은 전이를 기록하고, 그 이벤트에서 가장 높은 event_sequence 일 때만 현재 상태에 반영한다
 * (늦게 온 낮은 sequence 는 기록만). 부모 risk_event 행이 없으면(RAISED 보다 뒤 전이가 먼저 옴) 그 전이로 만든다(FK).
 * ack_id·close_id 는 그 행이 이미 있을 때만 잇는다(현장 버튼 확인 기록은 S15P31A101-121).
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class RiskEventRepository {

    private static final String ENSURE_EVENT = """
            INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity,
                confidence, needs_check, world_ids, zone_id, metrics, resume_count, retention_hold)
            VALUES (:eventId, 'LIVE', :gatewayId, :ruleId, :ruleVersion,
                CASE WHEN :transition = 'ACKNOWLEDGED' THEN 'RAISED' ELSE :transition END,
                :verification, :severity, :confidence, :needsCheck, CAST(:worldIds AS text[]), :zoneId,
                CAST(:metrics AS jsonb), 0, false)
            ON CONFLICT (event_id) DO NOTHING
            """;

    private static final String INSERT_UPDATE = """
            INSERT INTO risk_event_update (event_id, event_sequence, msg_id, gateway_id, journal_offset, transition,
                occurred_at, observation_at, evaluated_at, rule_id, rule_version, config_version, calibration_version,
                scene_version, source, severity, confidence, verification, needs_check, confidence_reasons, world_ids,
                zone_id, metrics, evidence_ref, resent, resumed, related_event_id, ack_id, close_id, escalation_id,
                delivery_mode, received_at)
            VALUES (:eventId, :seq, :msgId, :gatewayId, :journalOffset, :transition,
                :occurredAt, :observationAt, :evaluatedAt, :ruleId, :ruleVersion, :configVersion, :calibrationVersion,
                :sceneVersion, :source, :severity, :confidence, :verification, :needsCheck,
                CAST(:confidenceReasons AS text[]), CAST(:worldIds AS text[]),
                :zoneId, CAST(:metrics AS jsonb), CAST(:evidenceRef AS uuid), :resent, :resumed,
                (SELECT e.event_id FROM risk_event e WHERE e.event_id = CAST(:relatedEventId AS uuid)),
                (SELECT a.ack_id FROM event_ack a WHERE a.ack_id = CAST(:ackId AS uuid)),
                (SELECT c.close_id FROM event_close c WHERE c.close_id = CAST(:closeId AS uuid)),
                CAST(:escalationId AS uuid), :deliveryMode, :receivedAt)
            ON CONFLICT DO NOTHING
            """;

    /** 가장 높은 sequence 일 때만 바뀐다. ACKNOWLEDGED 는 lifecycle 을 바꾸지 않는다. resumed 는 다시 활성(3.10) */
    private static final String APPLY = """
            UPDATE risk_event SET
                lifecycle = CASE WHEN :transition = 'ACKNOWLEDGED' THEN lifecycle ELSE :transition END,
                gateway_id = :gatewayId, rule_version = :ruleVersion, verification = :verification, severity = :severity,
                confidence = :confidence, needs_check = :needsCheck, confidence_reasons = CAST(:confidenceReasons AS text[]),
                world_ids = CAST(:worldIds AS text[]), zone_id = :zoneId, metrics = CAST(:metrics AS jsonb),
                event_sequence = :seq, config_version = :configVersion, calibration_version = :calibrationVersion,
                scene_version = :sceneVersion,
                raised_at = CASE WHEN :transition = 'RAISED' THEN :occurredAt ELSE raised_at END,
                updated_at = :occurredAt,
                resolved_at = CASE WHEN :transition = 'RESOLVED' THEN :occurredAt
                                   WHEN CAST(:resumed AS boolean) THEN NULL ELSE resolved_at END,
                resume_count = resume_count + CASE WHEN CAST(:resumed AS boolean) THEN 1 ELSE 0 END,
                resolution = CASE WHEN :transition = 'RESOLVED' THEN CAST(:resolution AS jsonb)
                                  WHEN CAST(:resumed AS boolean) THEN NULL ELSE resolution END,
                related_event_id = COALESCE(
                    (SELECT e.event_id FROM risk_event e WHERE e.event_id = CAST(:relatedEventId AS uuid)), related_event_id),
                delivery = COALESCE(:delivery, delivery)
            WHERE event_id = :eventId AND (event_sequence IS NULL OR event_sequence < :seq)
            """;

    private static final String ENSURE_FROM_SNAPSHOT = """
            INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity,
                confidence, needs_check, world_ids, zone_id, metrics, resume_count, event_sequence, raised_at, retention_hold)
            VALUES (:eventId, 'LIVE', :gatewayId, :ruleId, :ruleVersion, :lifecycle, :verification, :severity, :confidence,
                :needsCheck, CAST(:worldIds AS text[]), :zoneId, '{}'::jsonb, 0, :seq, :raisedAt, false)
            ON CONFLICT (event_id) DO NOTHING
            """;

    private final NamedParameterJdbcTemplate jdbc;

    public RiskEventRepository(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * 전이 하나를 기록하고 현재 상태에 반영한다. 한 트랜잭션 안에서 부른다.
     * @param deliveryMode 동기화 중이면 AUDIT_ONLY, 아니면 REALTIME
     * @return 새로 기록했으면 true, 같은 전이(event_id·sequence 또는 msg_id 또는 journal_offset)가 이미 있으면 false
     */
    public boolean recordTransition(I1Envelope envelope, String deliveryMode, Instant receivedAt) {
        JsonNode p = envelope.payload();
        Integer seq = integer(p, "event_sequence");
        if (seq == null || seq < 1) {
            throw new InvalidI1Message("event_sequence 가 1 이상이 아니다");
        }
        JsonNode ack = p.get("ack");
        JsonNode resolution = p.get("resolution");
        JsonNode escalation = p.get("escalation");
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("eventId", requiredUuid(p, "event_id"))
                .addValue("seq", seq)
                .addValue("msgId", envelope.msgId())
                .addValue("gatewayId", requiredText(p, "gateway_id"))
                .addValue("journalOffset", requiredWhole(p, "journal_offset"))
                .addValue("transition", requiredText(p, "transition"))
                .addValue("occurredAt", requiredTime(p, "occurred_at"))
                .addValue("observationAt", requiredTime(p, "observation_time"))
                .addValue("evaluatedAt", requiredTime(p, "evaluated_at"))
                .addValue("ruleId", requiredText(p, "rule_id"))
                .addValue("ruleVersion", integer(p, "rule_version"))
                .addValue("configVersion", text(p, "config_version"))
                .addValue("calibrationVersion", text(p, "calibration_version"))
                .addValue("sceneVersion", text(p, "scene_version"))
                .addValue("source", text(p, "source"))
                .addValue("severity", text(p, "severity"))
                .addValue("confidence", text(p, "confidence"))
                .addValue("verification", text(p, "verification"))
                .addValue("needsCheck", bool(p, "needs_check"))
                .addValue("confidenceReasons", textArray(p, "confidence_reasons"))
                .addValue("worldIds", textArray(p, "world_ids"))
                .addValue("zoneId", text(p, "zone_id"))
                .addValue("metrics", json(p, "metrics"))
                .addValue("evidenceRef", uuidText(p, "evidence_ref"))
                .addValue("resent", Boolean.TRUE.equals(bool(p, "resent")))
                .addValue("resumed", Boolean.TRUE.equals(bool(p, "resumed")))
                .addValue("relatedEventId", uuidText(p, "related_event_id"))
                .addValue("ackId", absent(ack) ? null : uuidText(ack, "ack_id"))
                .addValue("closeId", absent(resolution) ? null : uuidText(resolution, "close_id"))
                .addValue("escalationId", absent(escalation) ? null : uuidText(escalation, "escalation_id"))
                .addValue("resolution", json(p, "resolution"))
                .addValue("deliveryMode", deliveryMode)
                .addValue("delivery", "REALTIME".equals(deliveryMode) ? deliveryMode : null)
                .addValue("receivedAt", utc(receivedAt));
        jdbc.update(ENSURE_EVENT, params);
        if (jdbc.update(INSERT_UPDATE, params) == 0) {
            return false;
        }
        jdbc.update(APPLY, params);
        return true;
    }

    /** UUID 형식을 검사하고 문자열로 넘긴다(SQL 에서 CAST). 형식이 틀리면 CAST 오류가 아니라 계약 위반으로 처리한다 */
    private static String uuidText(JsonNode parent, String field) {
        UUID value = uuid(parent, field);
        return value == null ? null : value.toString();
    }

    /** 스냅샷의 활성 이벤트가 EC2 에 없으면 스냅샷 값으로 만든다(받지 못한 전이를 저널이 이미 정리한 경우). @return 새로 만들었으면 true */
    public boolean ensureFromSnapshot(String gatewayId, JsonNode active) {
        return jdbc.update(ENSURE_FROM_SNAPSHOT, new MapSqlParameterSource()
                .addValue("eventId", requiredUuid(active, "event_id"))
                .addValue("gatewayId", gatewayId)
                .addValue("ruleId", requiredText(active, "rule_id"))
                .addValue("ruleVersion", integer(active, "rule_version"))
                .addValue("lifecycle", requiredText(active, "lifecycle"))
                .addValue("verification", text(active, "verification"))
                .addValue("severity", text(active, "severity"))
                .addValue("confidence", text(active, "confidence"))
                .addValue("needsCheck", bool(active, "needs_check"))
                .addValue("worldIds", textArray(active, "world_ids"))
                .addValue("zoneId", text(active, "zone_id"))
                .addValue("seq", integer(active, "event_sequence"))
                .addValue("raisedAt", time(active, "raised_at"))) > 0;
    }

    public void setDelivery(Collection<UUID> eventIds, String delivery) {
        if (eventIds.isEmpty()) {
            return;
        }
        jdbc.update("UPDATE risk_event SET delivery = :d WHERE event_id IN (:ids)",
                new MapSqlParameterSource().addValue("d", delivery).addValue("ids", eventIds));
    }

    /** EC2 는 활성으로 아는데 스냅샷에는 없는 이벤트 수(정합 점검 로그용) */
    public int countActiveNotIn(String gatewayId, Collection<UUID> activeIds) {
        String notIn = activeIds.isEmpty() ? "" : " AND event_id NOT IN (:ids)";
        Integer count = jdbc.queryForObject("SELECT count(*) FROM risk_event WHERE source = 'LIVE' AND gateway_id = :g"
                + " AND lifecycle <> 'RESOLVED'" + notIn,
                new MapSqlParameterSource().addValue("g", gatewayId).addValue("ids", activeIds), Integer.class);
        return count == null ? 0 : count;
    }
}
