package com.ssafy.simai.domain.event;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.util.List;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 이벤트 프레임(사진) 목록(S15P31A101-753, 명세 11.4·12.6·14). CRITICAL 이벤트의 프레임 주소·촬영 시각은 별도 테이블 없이
 * {@code vlm_rationale} 작업 입력(analysis_job.input.frames)에 있다(명세 11.4, {@link VlmRationaleIngestor}). 거기서 읽어 요청마다 짧게 사는
 * GET 서명 주소를 낸다. 서명 주소는 저장하지 않으며 만료되면 화면이 이 API 를 다시 부른다.
 * 이 이벤트 자신의 prefix(event-frames/&lt;event_id&gt;/)에 있는 객체만 서명한다(작업 입력의 다른 주소는 내지 않는다).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventFrameService {

    private static final String PREFIX = "event-frames/";

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectStorage storage;
    private final ObjectMapper mapper;

    public EventFrameService(NamedParameterJdbcTemplate jdbc, ObjectStorage storage, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.storage = storage;
        this.mapper = mapper;
    }

    /** 촬영 시각 순(같으면 카메라 순). 근거 작업이 없거나 저장소가 설정되지 않았으면 빈 목록 */
    public ObjectNode frames(UUID eventId) {
        MapSqlParameterSource params = new MapSqlParameterSource("e", eventId);
        if (jdbc.queryForList("SELECT 1 FROM risk_event WHERE event_id = :e", params).isEmpty()) {
            throw new BusinessException(ErrorCode.Event.EVENT_NOT_FOUND);
        }
        ObjectNode node = mapper.createObjectNode();
        node.put("event_id", eventId.toString());
        var out = node.putArray("frames");
        if (!storage.configured()) {
            return node;
        }
        String keyPrefix = PREFIX + eventId + "/";
        String uriPrefix = storage.uri(keyPrefix);
        List<String> inputs = jdbc.queryForList("""
                SELECT input -> 'frames' FROM analysis_job
                WHERE event_id = :e AND job_type = 'vlm_rationale' AND input -> 'frames' IS NOT NULL
                ORDER BY created_at LIMIT 1
                """, params, String.class);
        if (inputs.isEmpty() || inputs.getFirst() == null) {
            return node;
        }
        List<ObjectNode> frames = new java.util.ArrayList<>();
        for (JsonNode frame : mapper.readTree(inputs.getFirst())) {
            String uri = frame.path("frame_uri").asString("");
            String camera = frame.path("camera_id").asString("");
            if (camera.isEmpty() || !uri.startsWith(uriPrefix) || uri.length() == uriPrefix.length() || uri.contains("..")) {
                continue;
            }
            SignedRequest signed = storage.signGet(keyPrefix + uri.substring(uriPrefix.length()));
            ObjectNode item = mapper.createObjectNode();
            item.put("camera_id", camera);
            if (frame.has("estimated_capture_time_ms")) {
                item.put("capture_ms", frame.get("estimated_capture_time_ms").asLong());
            } else {
                item.putNull("capture_ms");
            }
            item.put("timestamp_source", frame.path("timestamp_source").asString(null));
            if (frame.has("timestamp_uncertainty_ms")) {
                item.put("timestamp_uncertainty_ms", frame.get("timestamp_uncertainty_ms").asLong());
            }
            item.put("url", signed.url());
            item.put("expires_at_ms", signed.expiresAt().toEpochMilli());
            frames.add(item);
        }
        frames.sort(java.util.Comparator.comparingLong((ObjectNode n) -> n.path("capture_ms").asLong(0))
                .thenComparing(n -> n.path("camera_id").asString()));
        out.addAll(frames);
        return node;
    }
}
