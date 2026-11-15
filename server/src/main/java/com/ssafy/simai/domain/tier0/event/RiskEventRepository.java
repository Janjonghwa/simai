package com.ssafy.simai.domain.tier0.event;

import static com.ssafy.simai.domain.tier0.i1.I1Json.*;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.time.Instant;
import java.util.Collection;
import java.util.Set;
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
 * ACKNOWLEDGED 전이의 확인(ack)은 event_ack 에 없으면 만든다(현장 버튼 확인·EC2 기록 유실, S15P31A101-121). 관제·반장 확인의
 * actor_id 는 user_id, 현장 버튼은 기기 ID 다(그 계정·기기가 없으면 비운다). 저널에 잡힌 sequence 를 journal_event_sequence 에 남긴다.
 * UPDATED 전이의 상향(escalation)은 event_escalation 에 반영하고 risk_event.escalations 에 시간순으로 누적 투영한다(S15P31A101-276).
 * close_id·escalation_id 는 그 행이 있을 때 잇는다.
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
                delivery_mode, received_at, journal_epoch)
            VALUES (:eventId, :seq, :msgId, :gatewayId, :journalOffset, :transition,
                :occurredAt, :observationAt, :evaluatedAt, :ruleId, :ruleVersion, :configVersion, :calibrationVersion,
                :sceneVersion, :source, :severity, :confidence, :verification, :needsCheck,
                CAST(:confidenceReasons AS text[]), CAST(:worldIds AS text[]),
                :zoneId, CAST(:metrics AS jsonb), CAST(:evidenceRef AS uuid), :resent, :resumed,
                CAST(:relatedEventId AS uuid),
                CAST(:ackId AS uuid),
                (SELECT c.close_id FROM event_close c WHERE c.close_id = CAST(:closeId AS uuid)),
                (SELECT e.escalation_id FROM event_escalation e WHERE e.escalation_id = CAST(:escalationId AS uuid)),
                :deliveryMode, :receivedAt, :epoch)
            ON CONFLICT DO NOTHING
            """;

    /**
     * 가장 높은 sequence 일 때만 바뀐다. ACKNOWLEDGED 는 lifecycle 을 바꾸지 않는다. resumed 는 다시 활성(3.10).
     * EC2 마감(S15P31A101-722)한 이벤트에 Tier 0 가 RAISED·UPDATED 를 보내면 Tier 0 가 아직 들고 있는 것이라 마감 투영(resolved_at·resolution)을 비운다.
     */
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
                                   WHEN CAST(:resumed AS boolean) THEN NULL
                                   WHEN :transition IN ('RAISED', 'UPDATED')
                                    AND resolution->>'kind' IN {SERVER_CLOSE_KINDS} THEN NULL
                                   ELSE resolved_at END,
                resume_count = resume_count + CASE WHEN CAST(:resumed AS boolean) THEN 1 ELSE 0 END,
                resolution = CASE WHEN :transition = 'RESOLVED' THEN CAST(:resolution AS jsonb)
                                  WHEN CAST(:resumed AS boolean) THEN NULL
                                  WHEN :transition IN ('RAISED', 'UPDATED')
                                   AND resolution->>'kind' IN {SERVER_CLOSE_KINDS} THEN NULL
                                  ELSE resolution END,
                proposed_severity = CASE
                    WHEN proposed_severity IS NOT NULL
                     AND (CASE proposed_severity WHEN 'INFO' THEN 0 WHEN 'CAUTION' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'CRITICAL' THEN 3 ELSE -1 END)
                      <= (CASE :severity WHEN 'INFO' THEN 0 WHEN 'CAUTION' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'CRITICAL' THEN 3 ELSE -1 END)
                    THEN NULL
                    ELSE proposed_severity
                END,
                related_event_id = COALESCE(
                    (SELECT e.event_id FROM risk_event e WHERE e.event_id = CAST(:relatedEventId AS uuid)), related_event_id),
                delivery = COALESCE(:delivery, delivery)
            WHERE event_id = :eventId AND (event_sequence IS NULL OR event_sequence < :seq)
            """.replace("{SERVER_CLOSE_KINDS}", ServerEventCloser.KINDS_SQL);

    private static final String ENSURE_ACK = """
            INSERT INTO event_ack (ack_id, event_id, channel, actor_role, actor_user_id, actor_device_id, acked_at)
            VALUES (CAST(:ackId AS uuid), :eventId, :ackChannel, :ackRole,
                CASE WHEN :ackChannel <> 'SITE_BUTTON'
                     THEN (SELECT u.user_id FROM app_user u WHERE u.user_id = CAST(:ackUserId AS bigint)) END,
                CASE WHEN :ackChannel = 'SITE_BUTTON'
                     THEN (SELECT d.device_id FROM device d WHERE d.device_id = :ackActor) END,
                :ackAt)
            ON CONFLICT (ack_id) DO NOTHING
            """;

    private static final String ACK_JOURNALED = """
            UPDATE event_ack SET journal_event_sequence = :seq
            WHERE ack_id = CAST(:ackId AS uuid) AND journal_event_sequence IS NULL
            """;

    private static final String ENSURE_ESCALATION = """
            INSERT INTO event_escalation (escalation_id, event_id, assessment_id, basis, target_severity, actor_user_id,
                requested_at, note, result, event_sequence, decided_at)
            SELECT CAST(:escalationId AS uuid), :eventId, va.assessment_id, :escalationBasis, :escalationTarget,
                u.user_id, :escalationAt, :escalationNote, 'ACCEPTED', :seq, :occurredAt
            FROM vlm_assessment va, app_user u
            WHERE va.job_id = :escalationJobId AND u.user_id = CAST(:escalationUserId AS bigint)
            ON CONFLICT (escalation_id) DO NOTHING
            """;

    private static final String ESCALATION_JOURNALED = """
            UPDATE event_escalation SET
                result = 'ACCEPTED',
                reject_reason = NULL,
                event_sequence = COALESCE(event_sequence, :seq),
                decided_at = COALESCE(decided_at, :occurredAt)
            WHERE escalation_id = CAST(:escalationId AS uuid)
            """;

    private static final String APPEND_ESCALATION = """
            UPDATE risk_event SET
                escalations = (
                    SELECT COALESCE(
                        jsonb_agg(elem ORDER BY (elem->>'requested_at')::bigint, elem->>'escalation_id'),
                        '[]'::jsonb
                    )
                    FROM (
                        SELECT elem
                        FROM jsonb_array_elements(risk_event.escalations) AS elem
                        WHERE elem->>'escalation_id' <> :escalationId
                        UNION ALL
                        SELECT CAST(:escalation AS jsonb)
                    ) merged
                )
            WHERE event_id = :eventId
            """;

    /** 같은 세대·offset 에 이미 있는 기록의 msg_id. 다른 msg_id 면 Tier 0 저널이 새로 시작된 것이다 */
    private static final String MSG_AT_OFFSET = """
            SELECT msg_id FROM risk_event_update
            WHERE gateway_id = :gatewayId AND journal_epoch = :epoch AND journal_offset = :journalOffset
            """;

    /** RAISED 가 더 높은 sequence 보다 늦게 와도 처음 시각은 채운다 */
    private static final String FILL_RAISED_AT = """
            UPDATE risk_event SET raised_at = :occurredAt
            WHERE event_id = :eventId AND raised_at IS NULL AND :transition = 'RAISED'
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

    public enum Recorded {
        /** 새로 기록했고 현재 상태(risk_event)도 바꿨다 */
        APPLIED,
        /** 새로 기록했지만 더 높은 sequence 가 이미 반영돼 현재 상태는 그대로다(늦게 온 전이) */
        HISTORY_ONLY,
        /** 같은 전이(event_id·sequence 또는 msg_id)가 이미 있다 */
        DUPLICATE,
        /** 같은 세대·offset 에 다른 메시지가 있다. Tier 0 저널이 새로 시작됐다 */
        OFFSET_CONFLICT;

        /** risk_event_update 에 새 행을 남겼다 */
        public boolean inserted() {
            return this == APPLIED || this == HISTORY_ONLY;
        }
    }

    /**
     * 전이 하나를 기록하고 현재 상태에 반영한다. 한 트랜잭션 안에서 부른다.
     * @param deliveryMode 동기화 중이면 AUDIT_ONLY, 아니면 REALTIME
     * @param epoch 지금 저널 세대(V4 journal_epoch)
     */
    public Recorded recordTransition(I1Envelope envelope, String deliveryMode, Instant receivedAt, int epoch) {
        JsonNode p = envelope.payload();
        requireStateFields(p);
        Integer seq = integer(p, "event_sequence");
        if (seq == null || seq < 1) {
            throw new InvalidI1Message("event_sequence 가 1 이상이 아니다");
        }
        String transition = requiredText(p, "transition");
        boolean resumed = Boolean.TRUE.equals(bool(p, "resumed"));
        JsonNode ack = p.get("ack");
        JsonNode resolution = p.get("resolution");
        JsonNode escalation = p.get("escalation");
        boolean hasAck = !absent(ack);
        boolean hasEscalation = !absent(escalation);
        if (hasAck) {
            validateAck(ack);
        }
        if (hasEscalation) {
            validateEscalation(envelope.schemaVersion(), transition, resumed, escalation);
        }
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("eventId", requiredUuid(p, "event_id"))
                .addValue("seq", seq)
                .addValue("msgId", envelope.msgId())
                .addValue("gatewayId", requiredText(p, "gateway_id"))
                .addValue("journalOffset", requiredWhole(p, "journal_offset"))
                .addValue("transition", transition)
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
                .addValue("resumed", resumed)
                .addValue("relatedEventId", uuidText(p, "related_event_id"))
                .addValue("ackId", hasAck ? uuidText(ack, "ack_id") : null)
                .addValue("ackChannel", hasAck ? text(ack, "channel") : null)
                .addValue("ackRole", hasAck ? text(ack, "actor_role") : null)
                .addValue("ackActor", hasAck ? text(ack, "actor_id") : null)
                .addValue("ackUserId", hasAck ? userId(text(ack, "actor_id")) : null)
                .addValue("ackAt", hasAck ? time(ack, "acked_at") : null)
                .addValue("closeId", absent(resolution) ? null : uuidText(resolution, "close_id"))
                .addValue("escalationId", hasEscalation ? uuidText(escalation, "escalation_id") : null)
                .addValue("escalationBasis", hasEscalation ? text(escalation, "basis") : null)
                .addValue("escalationJobId", hasEscalation ? text(escalation, "assessment_job_id") : null)
                .addValue("escalationTarget", hasEscalation ? text(escalation, "target_severity") : null)
                .addValue("escalationUserId", hasEscalation ? userId(text(escalation, "actor_id")) : null)
                .addValue("escalationAt", hasEscalation ? time(escalation, "requested_at") : null)
                .addValue("escalationNote", hasEscalation ? text(escalation, "note") : null)
                .addValue("escalation", json(p, "escalation"))
                .addValue("resolution", json(p, "resolution"))
                .addValue("deliveryMode", deliveryMode)
                .addValue("delivery", "REALTIME".equals(deliveryMode) ? deliveryMode : null)
                .addValue("receivedAt", utc(receivedAt))
                .addValue("epoch", epoch);
        jdbc.update(ENSURE_EVENT, params);
        if (hasAck) {
            jdbc.update(ENSURE_ACK, params);
        }
        if (hasEscalation && params.getValue("escalationUserId") != null) {
            jdbc.update(ENSURE_ESCALATION, params);
        }
        if (jdbc.update(INSERT_UPDATE, params) == 0) {
            if (hasAck) {
                // 이미 기록된 전이를 다시 받아도(배포 전 기록·재전송) 저널에 잡혔다는 것은 남긴다
                jdbc.update(ACK_JOURNALED, params);
            }
            if (hasEscalation) {
                jdbc.update(ESCALATION_JOURNALED, params);
                jdbc.update(APPEND_ESCALATION, params);
            }
            java.util.List<UUID> existing = jdbc.queryForList(MSG_AT_OFFSET, params, UUID.class);
            return existing.isEmpty() || existing.contains(envelope.msgId()) ? Recorded.DUPLICATE : Recorded.OFFSET_CONFLICT;
        }
        boolean applied = jdbc.update(APPLY, params) > 0;
        jdbc.update(FILL_RAISED_AT, params);
        if (hasAck) {
            jdbc.update(ACK_JOURNALED, params);
        }
        if (hasEscalation) {
            jdbc.update(ESCALATION_JOURNALED, params);
            jdbc.update(APPEND_ESCALATION, params);
        }
        return applied ? Recorded.APPLIED : Recorded.HISTORY_ONLY;
    }

    private static final Set<String> ACK_CHANNELS = Set.of("CONTROL_ROOM", "FOREMAN_PWA", "SITE_BUTTON");
    private static final Set<String> ACK_ROLES = Set.of("ADMIN", "FOREMAN", "SITE");
    private static final Set<String> ESCALATION_FIELDS = Set.of(
            "escalation_id", "basis", "assessment_job_id", "target_severity",
            "channel", "actor_role", "actor_id", "requested_at", "note");
    private static final Set<String> ESCALATION_TARGETS = Set.of("CRITICAL", "HIGH", "CAUTION");
    private static final Set<String> PRE_ESCALATION_VERSIONS = Set.of("1.0.0", "1.1.0", "2.0.0");

    /** 계약 operator_ack 필수 칸·값. 틀리면 DB 제약 오류(저장 실패)가 아니라 계약 위반으로 버린다(건너뛰기 대상) */
    private static void validateAck(JsonNode ack) {
        requiredUuid(ack, "ack_id");
        if (!ACK_CHANNELS.contains(requiredText(ack, "channel"))) {
            throw new InvalidI1Message("ack.channel 이 계약 값이 아니다");
        }
        if (!ACK_ROLES.contains(requiredText(ack, "actor_role"))) {
            throw new InvalidI1Message("ack.actor_role 이 계약 값이 아니다");
        }
        requiredText(ack, "actor_id");
        requiredTime(ack, "acked_at");
    }

    /** 계약 operator_escalation 및 risk_event_transition 2.1.0 의 escalation 제약 검사 */
    private static void validateEscalation(String schemaVersion, String transition, boolean resumed, JsonNode esc) {
        if (schemaVersion != null && PRE_ESCALATION_VERSIONS.contains(schemaVersion)) {
            throw new InvalidI1Message("escalation 은 schema_version 2.1.0 이상에서만 허용된다");
        }
        if (!"UPDATED".equals(transition) || resumed) {
            throw new InvalidI1Message("escalation 은 resumed 가 false 인 UPDATED 전이에서만 허용된다");
        }
        if (!esc.isObject()) {
            throw new InvalidI1Message("escalation 이 객체가 아니다");
        }
        for (String field : esc.propertyNames()) {
            if (!ESCALATION_FIELDS.contains(field)) {
                throw new InvalidI1Message("escalation 에 허용되지 않은 필드가 있다: " + field);
            }
        }
        requiredUuid(esc, "escalation_id");
        if (!"VLM_PROPOSAL".equals(requiredText(esc, "basis"))) {
            throw new InvalidI1Message("escalation.basis 가 VLM_PROPOSAL 이 아니다");
        }
        requiredText(esc, "assessment_job_id");
        if (!ESCALATION_TARGETS.contains(requiredText(esc, "target_severity"))) {
            throw new InvalidI1Message("escalation.target_severity 가 계약 값이 아니다");
        }
        if (!"CONTROL_ROOM".equals(requiredText(esc, "channel"))) {
            throw new InvalidI1Message("escalation.channel 이 CONTROL_ROOM 이 아니다");
        }
        if (!"ADMIN".equals(requiredText(esc, "actor_role"))) {
            throw new InvalidI1Message("escalation.actor_role 이 ADMIN 이 아니다");
        }
        requiredText(esc, "actor_id");
        requiredTime(esc, "requested_at");
        if (esc.has("note") && !esc.get("note").isNull()) {
            String note = requiredText(esc, "note");
            if (note.length() > 500) {
                throw new InvalidI1Message("escalation.note 가 500자를 넘는다");
            }
        }
    }

    /** actor_id 를 user_id 로 읽는다. 숫자("3") 또는 계약 예시 형식("user-0003"), 그 밖은 null */
    static Long userId(String actorId) {
        if (actorId == null) {
            return null;
        }
        String digits = actorId.startsWith("user-") ? actorId.substring(5) : actorId;
        if (digits.isEmpty() || digits.length() > 18 || !digits.chars().allMatch(Character::isDigit)) {
            return null;
        }
        return Long.parseLong(digits);
    }

    /** UUID 형식을 검사하고 문자열로 넘긴다(SQL 에서 CAST). 형식이 틀리면 CAST 오류가 아니라 계약 위반으로 처리한다 */
    private static String uuidText(JsonNode parent, String field) {
        UUID value = uuid(parent, field);
        return value == null ? null : value.toString();
    }

    /**
     * risk_event 의 NOT NULL 칸에 들어가는 계약 필수 필드(risk_event_transition·event_state_snapshot 공통)를 먼저 본다.
     * 빠지면 DB 제약 오류(저장 실패, offset 미확인으로 같은 전이를 끝없이 다시 받음)가 아니라 계약 위반으로 거부한다.
     */
    static void requireStateFields(JsonNode p) {
        requiredUuid(p, "event_id");
        requiredText(p, "rule_id");
        if (integer(p, "rule_version") == null) {
            throw new InvalidI1Message("rule_version 이 비었다");
        }
        requiredText(p, "verification");
        requiredText(p, "severity");
        requiredText(p, "confidence");
        if (bool(p, "needs_check") == null) {
            throw new InvalidI1Message("needs_check 가 비었다");
        }
        if (textArray(p, "world_ids") == null) {
            throw new InvalidI1Message("world_ids 가 비었다");
        }
    }

    /** 스냅샷의 활성 이벤트가 EC2 에 없으면 스냅샷 값으로 만든다(받지 못한 전이를 저널이 이미 정리한 경우). @return 새로 만들었으면 true */
    public boolean ensureFromSnapshot(String gatewayId, JsonNode active) {
        requireStateFields(active);
        requiredText(active, "lifecycle");
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

    /** 그중 delivery 가 아직 정해지지 않은 이벤트(끝나지 못한 이전 동기화에서 만든 것 등) */
    public java.util.Set<UUID> withoutDelivery(Collection<UUID> eventIds) {
        if (eventIds.isEmpty()) {
            return java.util.Set.of();
        }
        return java.util.Set.copyOf(jdbc.queryForList(
                "SELECT event_id FROM risk_event WHERE event_id IN (:ids) AND delivery IS NULL",
                new MapSqlParameterSource("ids", eventIds), UUID.class));
    }

    public void setDelivery(Collection<UUID> eventIds, String delivery) {
        if (eventIds.isEmpty()) {
            return;
        }
        jdbc.update("UPDATE risk_event SET delivery = :d WHERE event_id IN (:ids)",
                new MapSqlParameterSource().addValue("d", delivery).addValue("ids", eventIds));
    }

    /**
     * 스냅샷이 활성으로 실은 이벤트를 EC2 가 마감해 뒀으면(S15P31A101-722) Tier 0 쪽을 따라 되돌린다. 마감 기록(risk_event_server_close)은 그대로 둔다.
     * @return 되돌렸으면 true
     */
    public boolean reopenServerClosed(String gatewayId, JsonNode active) {
        String lifecycle = text(active, "lifecycle");
        if (!"RAISED".equals(lifecycle) && !"UPDATED".equals(lifecycle)) {
            return false;
        }
        return jdbc.update("UPDATE risk_event SET lifecycle = :lifecycle, resolved_at = NULL, resolution = NULL"
                + " WHERE event_id = :eventId AND source = 'LIVE' AND gateway_id = :g AND lifecycle = 'RESOLVED'"
                + " AND resolution->>'kind' IN " + ServerEventCloser.KINDS_SQL,
                new MapSqlParameterSource()
                        .addValue("lifecycle", lifecycle)
                        .addValue("eventId", requiredUuid(active, "event_id"))
                        .addValue("g", gatewayId)) > 0;
    }
}
