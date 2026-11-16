package com.ssafy.simai.domain.video;

import static com.ssafy.simai.domain.tier0.i1.I1Json.utc;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.domain.upload.ClipFormat;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * video_segment(Tier 0 → EC2, I1, S15P31A101-330) → video_segment 한 행. 보낸 기록은 고치지 않는다(계약, AGENTS 6).
 * 계약 스키마 전체로 검사하고, 객체 키가 EC2 가 이 조각에 발급한 업로드(upload_grant, VIDEO_SEGMENT)와 형식·크기·해시까지 같은지 맞춘 뒤
 * 발급을 쓰인 것으로 표시한다(이벤트 클립 MediaClipIngestor 와 같은 방식). 이벤트와 묶이지 않아 기다릴 앞 메시지가 없다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class VideoSegmentIngestor implements I1MessageIngestor {

    private static final String SCHEMA = "messages/video_segment";
    /** 보관 기간(11.5). Tier 0 시계 반올림·설정 지연을 하루까지 봐준다 */
    static final Duration RETENTION = Duration.ofDays(14);
    private static final Duration RETENTION_TOLERANCE = Duration.ofDays(1);
    /** PostgreSQL timestamptz 가 받는 범위 안(9999-12-31) */
    private static final long MAX_EPOCH_MS = 253_402_300_799_999L;
    private static final List<String> TIMES = List.of("segment_start_capture_ms", "segment_end_capture_ms", "created_at_ms",
            "delete_after_ms", "uploaded_at_ms");

    private static final String INSERT = """
            INSERT INTO video_segment (segment_id, gateway_id, camera_id, stream_epochs, segment_start_capture_at,
                segment_end_capture_at, frame_count, starts_with_keyframe, has_gaps, content_type, container, size_bytes, sha256,
                face_redaction, segment_created_at, delete_after, object_key, uploaded_at, upload_attempts, received_at)
            VALUES (:segmentId, :gatewayId, :cameraId, CAST(:streamEpochs AS bigint[]), :start, :end, :frameCount, :keyframe,
                :hasGaps, :contentType, :container, :sizeBytes, :sha256, :faceRedaction, :createdAt, :deleteAfter, :objectKey,
                :uploadedAt, :uploadAttempts, :receivedAt)
            ON CONFLICT (segment_id) DO NOTHING
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;

    VideoSegmentIngestor(NamedParameterJdbcTemplate jdbc, ContractSchemas schemas, ObjectMapper mapper, TransactionTemplate transaction) {
        this.jdbc = jdbc;
        this.schemas = schemas;
        this.mapper = mapper;
        this.transaction = transaction;
    }

    @Override
    public String msgType() {
        return "video_segment";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1);
    }

    @Override
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        List<String> problems = schemas.violations(SCHEMA, mapper.writeValueAsString(envelope.toContractJson(mapper)));
        if (!problems.isEmpty()) {
            // 검사 문구는 경로와 규칙만 담는다(값 없음)
            throw new InvalidI1Message("video_segment 가 계약에 맞지 않는다: " + problems);
        }
        JsonNode p = envelope.payload();
        if (!envelope.sourceId().equals(p.get("gateway_id").asString())) {
            throw new InvalidI1Message("gateway_id 가 source_id 와 다르다");
        }
        for (String field : TIMES) {
            // 계약 timestamp_ms 는 상한이 없다. DB 가 받지 못하는 시각은 DB 오류가 아니라 계약 위반으로 버린다
            if (p.get(field).asLong() > MAX_EPOCH_MS) {
                throw new InvalidI1Message(field + " 이 받을 수 있는 시각을 넘는다");
            }
        }
        if (!p.get("frame_count").canConvertToInt() || p.get("stream_epochs").valueStream().anyMatch(v -> !v.canConvertToLong())) {
            throw new InvalidI1Message("frame_count·stream_epochs 가 정수 범위를 넘는다");
        }
        long start = p.get("segment_start_capture_ms").asLong();
        long end = p.get("segment_end_capture_ms").asLong();
        if (end < start) {
            throw new InvalidI1Message("segment_end_capture_ms 가 segment_start_capture_ms 보다 이르다");
        }
        if (end - start > VideoService.MAX_SEGMENT.toMillis()) {
            // 목록 질의는 조각 길이를 이만큼으로 보고 시작 시각을 거슬러 찾는다. 더 긴 조각은 목록에서 빠지므로 받지 않는다
            throw new InvalidI1Message("조각이 " + VideoService.MAX_SEGMENT + " 보다 길다");
        }
        long keep = p.get("delete_after_ms").asLong() - p.get("created_at_ms").asLong();
        if (Math.abs(keep - RETENTION.toMillis()) > RETENTION_TOLERANCE.toMillis()) {
            // 계약: 삭제 예정일 = 자른 시각 + 14일. 크게 어긋나면 영구 보존되거나 곧바로 지워지므로 받지 않는다
            throw new InvalidI1Message("delete_after_ms 가 created_at_ms + 14일이 아니다");
        }
        String contentType = p.get("content_type").asString().strip().toLowerCase(Locale.ROOT);
        if (!ClipFormat.of(contentType).map(ClipFormat::container).orElse("").equals(p.get("container").asString())) {
            throw new InvalidI1Message("content_type 과 container 가 맞지 않는다");
        }
        MapSqlParameterSource row = row(p, receivedAt).addValue("contentType", contentType);
        return transaction.execute(status -> {
            // 아직 행이 없는 조각은 FOR UPDATE 로 잠기지 않는다. 같은 조각을 잠가 재연결로 겹쳐 온 같은 기록을 한 줄로 세운다
            jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.video:' || :segmentId))", row);
            List<Map<String, Object>> existing = jdbc.queryForList(
                    "SELECT sha256, gateway_id, camera_id FROM video_segment WHERE segment_id = :segmentId", row);
            if (!existing.isEmpty()) {
                // 재전송(12.1)은 같은 기록이어야 한다. 같은 segment_id 에 다른 내용은 고치지 않고 버린다
                Map<String, Object> stored = existing.getFirst();
                if (!Objects.equals(stored.get("sha256"), row.getValue("sha256"))
                        || !Objects.equals(stored.get("gateway_id"), row.getValue("gatewayId"))
                        || !Objects.equals(stored.get("camera_id"), row.getValue("cameraId"))) {
                    throw new InvalidI1Message("같은 segment_id 에 다른 조각");
                }
                return Outcome.DUPLICATE;
            }
            claimUpload(p, contentType, receivedAt);
            jdbc.update(INSERT, row);
            return Outcome.STORED;
        });
    }

    /**
     * 객체 키는 EC2 가 이 조각에 발급한 것이어야 하고(키에 segment_id 가 있다), 발급에 묶은 형식·크기·해시가 기록과 같아야 한다.
     * 저장소는 서명에 묶은 내용만 받으므로, 같으면 올라온 객체가 기록이 말하는 그 내용이다. 확인되면 발급을 쓰인 것으로 표시한다.
     */
    private void claimUpload(JsonNode p, String contentType, Instant receivedAt) {
        String key = p.get("object_key").asString();
        String expectedPrefix = "video-segments/" + p.get("segment_id").asString() + ".";
        List<Map<String, Object>> grants = jdbc.queryForList("""
                SELECT purpose, content_type, size_bytes, sha256 FROM upload_grant WHERE object_key = :key FOR UPDATE
                """, new MapSqlParameterSource("key", key));
        if (!key.startsWith(expectedPrefix) || grants.isEmpty()) {
            throw new InvalidI1Message("object_key 가 이 조각에 발급한 업로드가 아니다");
        }
        Map<String, Object> grant = grants.getFirst();
        boolean same = "VIDEO_SEGMENT".equals(grant.get("purpose"))
                && contentType.equals(grant.get("content_type"))
                && p.get("size_bytes").asLong() == ((Number) grant.get("size_bytes")).longValue()
                && p.get("sha256").asString().equals("sha256:" + grant.get("sha256"));
        if (!same) {
            throw new InvalidI1Message("video_segment 의 형식·크기·해시가 발급한 업로드와 다르다");
        }
        jdbc.update("UPDATE upload_grant SET used_at = :usedAt WHERE object_key = :key AND used_at IS NULL",
                new MapSqlParameterSource("key", key).addValue("usedAt", utc(receivedAt)));
    }

    private static MapSqlParameterSource row(JsonNode p, Instant receivedAt) {
        // stream_epoch 는 상한 없는 정수다. 노트북 중계는 세션 시작 UTC ms(약 1.79e12)부터 세므로 int 로는 담지 못한다(media_clip -754 와 같음)
        List<Long> epochs = new ArrayList<>();
        p.get("stream_epochs").values().forEach(v -> epochs.add(v.asLong()));
        return new MapSqlParameterSource()
                .addValue("segmentId", I1Json.requiredUuid(p, "segment_id"))
                .addValue("gatewayId", p.get("gateway_id").asString())
                .addValue("cameraId", p.get("camera_id").asString())
                .addValue("streamEpochs", I1Json.arrayLiteral(epochs))
                .addValue("start", I1Json.requiredTime(p, "segment_start_capture_ms"))
                .addValue("end", I1Json.requiredTime(p, "segment_end_capture_ms"))
                .addValue("frameCount", p.get("frame_count").asInt())
                .addValue("keyframe", p.get("starts_with_keyframe").asBoolean())
                .addValue("hasGaps", p.get("has_gaps").asBoolean())
                .addValue("container", p.get("container").asString())
                .addValue("sizeBytes", p.get("size_bytes").asLong())
                .addValue("sha256", p.get("sha256").asString())
                .addValue("faceRedaction", p.get("face_redaction").asString())
                .addValue("createdAt", I1Json.requiredTime(p, "created_at_ms"))
                .addValue("deleteAfter", I1Json.requiredTime(p, "delete_after_ms"))
                .addValue("objectKey", p.get("object_key").asString())
                .addValue("uploadedAt", I1Json.requiredTime(p, "uploaded_at_ms"))
                .addValue("uploadAttempts", I1Json.integer(p, "upload_attempts"))
                .addValue("receivedAt", utc(receivedAt));
    }
}
