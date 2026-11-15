package com.ssafy.simai.domain.clip;

import static com.ssafy.simai.domain.tier0.i1.I1Json.utc;

import com.ssafy.simai.domain.clip.ClipDeferral.Deferrable;
import com.ssafy.simai.domain.clip.ClipDeferral.WaitingForEarlier;
import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.domain.upload.ClipFormat;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
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
 * media_clip(Tier 0 → EC2, I1, 명세 12.3·11.3·11.5) → media_clip 한 행. 보낸 기록은 고치지 않는다(계약, AGENTS 6).
 * 계약 스키마 전체로 검사하고, 클립을 만든 전이(event_id, event_sequence)·위험 구간이 그 Tier 0 의 LIVE 이벤트에 있는지, 객체 키가 EC2 가
 * 이 클립에 발급한 업로드(upload_grant, S15P31A101-69)와 형식·크기·해시·이벤트까지 같은지 맞춘 뒤 발급을 쓰인 것으로 표시한다.
 * 전이를 아직 받지 못했으면 버리지 않고 보관했다가 다시 처리한다(ClipDeferral, 클립 기록은 Tier 0 가 다시 보내지 않는다).
 * CRITICAL 클립은 보존 표시가 켜져 있어야 하고(11.5 자동 보존) 보존 이력(media_clip_retention)에 CRITICAL_AUTO 로도 남긴다.
 * 같은 위험 구간·카메라는 잠가 한 번에 하나씩 처리한다(재연결로 같은 기록이 겹쳐 와도 중복으로 끝나게).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class MediaClipIngestor implements Deferrable {

    private static final Logger log = LoggerFactory.getLogger(MediaClipIngestor.class);
    private static final String SCHEMA = "messages/media_clip";

    private static final String INSERT = """
            INSERT INTO media_clip (clip_id, event_id, gateway_id, event_sequence, resume_count, rule_id, severity, source, camera_id,
                stream_epochs, anchor_capture_at, requested_start_at, requested_end_at, clip_start_capture_at, clip_end_capture_at,
                frame_count, starts_with_keyframe, truncated_start, truncated_end, has_gaps, content_type, container, size_bytes,
                sha256, face_redaction, redactor_version, clip_created_at, delete_after, retention_hold, object_key, uploaded_at,
                spooled, upload_attempts, received_at)
            VALUES (:clipId, :eventId, :gatewayId, :eventSequence, :resumeCount, :ruleId, :severity, 'LIVE', :cameraId,
                CAST(:streamEpochs AS bigint[]), :anchor, :requestedStart, :requestedEnd, :clipStart, :clipEnd,
                :frameCount, :keyframe, :truncatedStart, :truncatedEnd, :hasGaps, :contentType, :container, :sizeBytes,
                :sha256, :faceRedaction, :redactorVersion, :createdAt, :deleteAfter, :retentionHold, :objectKey, :uploadedAt,
                :spooled, :uploadAttempts, :receivedAt)
            ON CONFLICT (clip_id) DO NOTHING
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;
    private final ClipDeferral deferral;

    MediaClipIngestor(NamedParameterJdbcTemplate jdbc, ContractSchemas schemas, ObjectMapper mapper, TransactionTemplate transaction,
            ClipDeferral deferral) {
        this.jdbc = jdbc;
        this.schemas = schemas;
        this.mapper = mapper;
        this.transaction = transaction;
        this.deferral = deferral;
    }

    @Override
    public String msgType() {
        return "media_clip";
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
            // 검사 문구는 경로와 규칙만 담는다(값 없음)
            throw new InvalidI1Message("media_clip 이 계약에 맞지 않는다: " + problems);
        }
        JsonNode p = envelope.payload();
        if (!envelope.sourceId().equals(p.get("gateway_id").asString())) {
            throw new InvalidI1Message("gateway_id 가 source_id 와 다르다");
        }
        if (p.get("clip_end_capture_ms").asLong() < p.get("clip_start_capture_ms").asLong()) {
            throw new InvalidI1Message("clip_end_capture_ms 가 clip_start_capture_ms 보다 이르다");
        }
        String contentType = p.get("content_type").asString().strip().toLowerCase(Locale.ROOT);
        if (!ClipFormat.of(contentType).map(ClipFormat::container).orElse("").equals(p.get("container").asString())) {
            throw new InvalidI1Message("content_type 과 container 가 맞지 않는다");
        }
        String severity = p.get("severity").asString();
        boolean hold = p.get("retention_hold").asBoolean();
        if ("CRITICAL".equals(severity) && !hold) {
            // CRITICAL 이벤트 연결분은 자동 보존이다(11.5, 계약). 꺼진 채 저장하면 삭제 작업이 지운다
            log.warn("CRITICAL 클립의 retention_hold 가 false 라 켜서 저장: clip_id={}", p.get("clip_id").asString());
            hold = true;
        }
        MapSqlParameterSource row = row(p, receivedAt).addValue("contentType", contentType).addValue("retentionHold", hold);
        boolean autoRetain = hold && "CRITICAL".equals(severity);
        WaitingForEarlier[] waiting = new WaitingForEarlier[1];
        Outcome outcome = transaction.execute(status -> {
            // 아직 행이 없는 클립은 FOR UPDATE 로 잠기지 않는다. 같은 위험 구간·카메라를 잠가 동시에 온 같은 기록을 한 줄로 세운다
            jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.clip:' || :eventId || ':' || :resumeCount || ':' || :cameraId))",
                    row);
            List<Map<String, Object>> existing = jdbc.queryForList("""
                    SELECT sha256, gateway_id, event_id, resume_count, camera_id FROM media_clip WHERE clip_id = :clipId
                    """, row);
            if (!existing.isEmpty()) {
                // 재전송(12.1)은 같은 기록이어야 한다. 같은 clip_id 에 다른 내용은 고치지 않고 버린다
                if (!sameClip(existing.getFirst(), row)) {
                    throw new InvalidI1Message("같은 clip_id 에 다른 클립");
                }
                return Outcome.DUPLICATE;
            }
            try {
                checkEvent(envelope, row);
            } catch (WaitingForEarlier w) {
                // 잠금을 쥔 채 보관한다. 잠금을 놓은 뒤 보관하면 그 사이 이벤트 뒤에 온 재전송이 늦은 받은 시각으로 먼저 저장될 수 있다
                waiting[0] = w.parked(deferral.park(envelope, receivedAt, w.getMessage()));
                return null;
            }
            // received_at 은 처음 받은 시각이다(불변식, ClipDeferral.takeFirstReceipt). 보관 행은 이 저장과 함께 지운다
            Instant firstReceivedAt = deferral.takeFirstReceipt(envelope.msgId(), (UUID) row.getValue("clipId"), receivedAt);
            row.addValue("receivedAt", utc(firstReceivedAt));
            claimUpload(p, contentType, firstReceivedAt);
            if (!jdbc.queryForList("""
                    SELECT 1 FROM media_clip WHERE event_id = :eventId AND resume_count = :resumeCount AND camera_id = :cameraId
                    """, row).isEmpty()) {
                // (event_id, resume_count, camera_id)마다 클립은 하나다(계약)
                throw new InvalidI1Message("같은 위험 구간·카메라에 다른 clip_id");
            }
            if (jdbc.update(INSERT, row) == 0) {
                // 다른 위험 구간 잠금을 잡은 요청이 같은 clip_id 를 먼저 썼다
                throw new InvalidI1Message("같은 clip_id 에 다른 클립");
            }
            if (autoRetain) {
                ClipRetentions.recordTier0(jdbc, (UUID) row.getValue("clipId"), (UUID) row.getValue("eventId"),
                        (String) row.getValue("gatewayId"), (int) row.getValue("resumeCount"), severity, "CRITICAL_AUTO",
                        I1Json.requiredTime(p, "created_at_ms"), envelope.msgId());
            }
            return Outcome.STORED;
        });
        if (waiting[0] != null) {
            throw waiting[0];
        }
        return outcome;
    }

    /**
     * 클립은 그 Tier 0 의 LIVE 이벤트에만 있고(계약, AGENTS 5), 클립을 만든 전이(event_sequence)와 그 위험 구간(resume_count)이
     * 이미 와 있어야 한다. 아직이면 기다린다(WaitingForEarlier). 클립 등급은 그 전이의 등급이다(계약: 발생·재개 또는 자르기 전 상향 전이).
     */
    private void checkEvent(I1Envelope envelope, MapSqlParameterSource row) {
        List<Map<String, Object>> event = jdbc.queryForList(
                "SELECT source, gateway_id, resume_count FROM risk_event WHERE event_id = :eventId", row);
        if (event.isEmpty()) {
            throw new WaitingForEarlier("이벤트 전이를 아직 받지 못했다");
        }
        Map<String, Object> e = event.getFirst();
        if (!"LIVE".equals(e.get("source")) || !envelope.sourceId().equals(e.get("gateway_id"))) {
            throw new InvalidI1Message("media_clip 의 이벤트가 이 Tier 0 의 LIVE 이벤트가 아니다");
        }
        if (((Number) e.get("resume_count")).intValue() < (int) row.getValue("resumeCount")) {
            throw new WaitingForEarlier("재개 전이를 아직 받지 못했다");
        }
        List<String> transition = jdbc.queryForList("""
                SELECT severity FROM risk_event_update WHERE event_id = :eventId AND event_sequence = :eventSequence
                """, row, String.class);
        if (transition.isEmpty()) {
            throw new WaitingForEarlier("클립을 만든 전이를 아직 받지 못했다");
        }
        if (!transition.getFirst().equals(row.getValue("severity"))) {
            throw new InvalidI1Message("media_clip 의 등급이 클립을 만든 전이의 등급과 다르다");
        }
    }

    private static boolean sameClip(Map<String, Object> stored, MapSqlParameterSource row) {
        return Objects.equals(stored.get("sha256"), row.getValue("sha256"))
                && Objects.equals(stored.get("gateway_id"), row.getValue("gatewayId"))
                && Objects.equals(stored.get("event_id"), row.getValue("eventId"))
                && ((Number) stored.get("resume_count")).intValue() == (int) row.getValue("resumeCount")
                && Objects.equals(stored.get("camera_id"), row.getValue("cameraId"));
    }

    /**
     * 객체 키는 EC2 가 이 클립에 발급한 것이어야 하고(키에 이벤트·clip_id 가 있다), 발급에 묶은 형식·크기·해시가 기록과 같아야 한다.
     * 저장소는 서명에 묶은 내용만 받으므로, 같으면 올라온 객체가 기록이 말하는 그 내용이다. 확인되면 발급을 쓰인 것으로 표시한다.
     */
    private void claimUpload(JsonNode p, String contentType, Instant receivedAt) {
        String key = p.get("object_key").asString();
        String expectedPrefix = "event-clips/" + p.get("event_id").asString() + "/" + p.get("clip_id").asString() + ".";
        List<Map<String, Object>> grants = jdbc.queryForList("""
                SELECT purpose, content_type, size_bytes, sha256, event_id::text AS event_id FROM upload_grant
                WHERE object_key = :key FOR UPDATE
                """, new MapSqlParameterSource("key", key));
        if (!key.startsWith(expectedPrefix) || grants.isEmpty()) {
            throw new InvalidI1Message("object_key 가 이 클립에 발급한 업로드가 아니다");
        }
        Map<String, Object> grant = grants.getFirst();
        boolean same = "EVENT_CLIP".equals(grant.get("purpose"))
                && contentType.equals(grant.get("content_type"))
                && p.get("size_bytes").asLong() == ((Number) grant.get("size_bytes")).longValue()
                && p.get("sha256").asString().equals("sha256:" + grant.get("sha256"))
                && p.get("event_id").asString().equals(grant.get("event_id"));
        if (!same) {
            throw new InvalidI1Message("media_clip 의 형식·크기·해시·이벤트가 발급한 업로드와 다르다");
        }
        jdbc.update("UPDATE upload_grant SET used_at = :usedAt WHERE object_key = :key AND used_at IS NULL",
                new MapSqlParameterSource("key", key).addValue("usedAt", utc(receivedAt)));
    }

    private static MapSqlParameterSource row(JsonNode p, Instant receivedAt) {
        // stream_epoch 는 상한 없는 정수다. 중계는 세션 시작 UTC ms(약 1.76e12)부터 세므로 int 로 읽으면 JsonNodeException 으로 버려졌다
        // (S15P31A101-754). 스키마가 정수·0 이상을 이미 봤고, long 을 넘는 값만 계약 위반으로 거부한다
        List<Long> epochs = new ArrayList<>();
        for (JsonNode v : p.get("stream_epochs").values()) {
            if (!v.isIntegralNumber() || !v.canConvertToLong()) {
                throw new InvalidI1Message("stream_epochs 가 int64 정수가 아니다");
            }
            epochs.add(v.longValue());
        }
        return new MapSqlParameterSource()
                .addValue("clipId", I1Json.requiredUuid(p, "clip_id"))
                .addValue("eventId", I1Json.requiredUuid(p, "event_id"))
                .addValue("gatewayId", p.get("gateway_id").asString())
                .addValue("eventSequence", I1Json.integer(p, "event_sequence"))
                .addValue("resumeCount", I1Json.integer(p, "resume_count"))
                .addValue("ruleId", p.get("rule_id").asString())
                .addValue("severity", p.get("severity").asString())
                .addValue("cameraId", p.get("camera_id").asString())
                .addValue("streamEpochs", I1Json.arrayLiteral(epochs))
                .addValue("anchor", I1Json.requiredTime(p, "anchor_capture_ms"))
                .addValue("requestedStart", I1Json.requiredTime(p, "requested_start_ms"))
                .addValue("requestedEnd", I1Json.requiredTime(p, "requested_end_ms"))
                .addValue("clipStart", I1Json.requiredTime(p, "clip_start_capture_ms"))
                .addValue("clipEnd", I1Json.requiredTime(p, "clip_end_capture_ms"))
                .addValue("frameCount", I1Json.integer(p, "frame_count"))
                .addValue("keyframe", p.get("starts_with_keyframe").asBoolean())
                .addValue("truncatedStart", p.get("truncated_start").asBoolean())
                .addValue("truncatedEnd", p.get("truncated_end").asBoolean())
                .addValue("hasGaps", p.get("has_gaps").asBoolean())
                .addValue("container", p.get("container").asString())
                .addValue("sizeBytes", p.get("size_bytes").asLong())
                .addValue("sha256", p.get("sha256").asString())
                .addValue("faceRedaction", p.get("face_redaction").asString())
                .addValue("redactorVersion", I1Json.text(p, "redactor_version"))
                .addValue("createdAt", I1Json.requiredTime(p, "created_at_ms"))
                .addValue("deleteAfter", I1Json.requiredTime(p, "delete_after_ms"))
                .addValue("objectKey", p.get("object_key").asString())
                .addValue("uploadedAt", I1Json.requiredTime(p, "uploaded_at_ms"))
                .addValue("spooled", I1Json.bool(p, "spooled"))
                .addValue("uploadAttempts", I1Json.integer(p, "upload_attempts"))
                .addValue("receivedAt", utc(receivedAt));
    }
}
