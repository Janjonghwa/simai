package com.ssafy.simai.domain.setup;

import com.ssafy.simai.domain.config.Versioned;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 변경 의심 구역 VLM 의견(S15P31A101-631, docs/design/S15P31A101-631-zone-vlm-opinion.md).
 * <ul>
 * <li>입력: 세팅 구역 초안 작업({@code vlm_zone_draft} 1.1.0)을 넣을 때, 이 세션의 높이 비교(-630)가 CHANGE_SUSPECTED 로 판정한 구역(높이 차이
 * 큰 순 8개)과 이전 관제 Scene 을 만든 세팅 세션의 CAMERA_FRAME·보정 참조를 싣는다. World 가 이어지지 않았거나 의심 구역이 없으면 싣지
 * 않는다(기존 초안 그대로). 이전 사진을 못 찾으면 구역만 싣고 워커가 NO_OPINION(NO_PREVIOUS_FRAMES)으로 답한다(지어내지 않음).</li>
 * <li>결과: 콜백의 {@code zone_opinions} 를 {@code setup_zone_opinion}(V66)에 추가만 한다. 사진 주소는 그 작업 입력의 전·후 프레임 주소다.</li>
 * <li>보기: 구역 계획 응답 항목의 {@code ai_opinion}. 전·후 CAMERA_FRAME 서명 GET 주소와 워커 크롭 박스를 싣고 화면이 잘라 보인다(사진을 새로
 * 올리지 않는다). 의견은 표시용 제안이고 구역 목록·needs_review·관제 구역을 바꾸지 않는다(AGENTS 규칙 4).</li>
 * </ul>
 */
@Component
public class SetupZoneOpinions {

    private static final Logger log = LoggerFactory.getLogger(SetupZoneOpinions.class);

    /** 한 작업에서 의견을 낼 구역 수 상한(계약 suspected_zones maxItems, 워커 ZoneOpinionConfig.max_zones 와 같다) */
    static final int MAX_ZONES = 8;
    /** 계약 previous_frames maxItems */
    private static final int MAX_FRAMES = 8;
    private static final int MAX_POLYGON = 64;
    private static final Pattern S3_URI = Pattern.compile("^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/\\S+$");
    private static final Pattern CALIBRATION_HASH = Pattern.compile("^sha256:[0-9a-f]{64}$");
    private static final Pattern CAMERA = Pattern.compile("^[A-Za-z0-9_-]{1,64}$");
    private static final Set<String> ACTIONS = Set.of("KEEP", "MODIFY", "REMOVE");
    private static final Set<String> REASONS = Set.of("NO_PREVIOUS_FRAMES", "PREVIOUS_FRAMES_UNAVAILABLE",
            "CURRENT_FRAMES_UNAVAILABLE", "CALIBRATION_UNAVAILABLE", "NOT_VISIBLE", "VLM_UNAVAILABLE", "VLM_FAILED",
            "TIME_BUDGET", "ZONE_LIMIT");

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final ObjectStorage storage;

    public SetupZoneOpinions(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, ObjectStorage storage) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.storage = storage;
    }

    // ------------------------------------------------------------------------------------------------ 작업 입력

    /**
     * 초안 작업 입력에 변경 의심 구역과 이전 세팅 사진·보정을 싣는다. 실었으면 true(작업 시간 상한을 늘린다).
     */
    boolean addOpinionInput(UUID setupSessionId, String siteId, ObjectNode input) {
        record Session(boolean reuse, boolean anchored, String zoneHeightChanges, String previousScene) { }
        Session session = jdbc.query("""
                SELECT s.mode = 'REUSE' AS reuse,
                       COALESCE((s.world_anchor ->> 'accepted')::boolean, false) AS anchored,
                       s.zone_height_changes::text AS zhc,
                       COALESCE(j.input -> 'previous_scene' ->> 'scene_version',
                                s.zone_height_changes ->> 'previous_scene_version') AS previous_scene
                FROM setup_session s LEFT JOIN analysis_job j ON j.job_id = s.job_id
                WHERE s.setup_session_id = :id AND s.site_id = :site
                """, new MapSqlParameterSource("id", setupSessionId).addValue("site", siteId),
                (rs, i) -> new Session(rs.getBoolean("reuse"), rs.getBoolean("anchored"), rs.getString("zhc"),
                        rs.getString("previous_scene"))).stream().findFirst().orElse(null);
        // 이전 모델 재사용은 초안 작업이 없고, World 가 이어지지 않았으면 같은 좌표로 비교할 수 없다(AGENTS 규칙 2·3)
        if (session == null || session.reuse() || !session.anchored() || session.zoneHeightChanges() == null) {
            return false;
        }
        ArrayNode zones = suspectedZones(siteId, mapper.readTree(session.zoneHeightChanges()));
        if (zones.isEmpty()) {
            return false;
        }
        input.set("suspected_zones", zones);
        if (session.previousScene() != null) {
            addPreviousSetup(setupSessionId, siteId, session.previousScene(), input);
        }
        return true;
    }

    /** CHANGE_SUSPECTED 구역을 높이 차이 큰 순으로, 지금 관제 구역의 다각형·높이와 함께 */
    private ArrayNode suspectedZones(String siteId, JsonNode zoneHeightChanges) {
        List<JsonNode> suspected = new ArrayList<>();
        for (JsonNode item : zoneHeightChanges.path("zones")) {
            if ("CHANGE_SUSPECTED".equals(item.path("status").asString("")) && item.path("zone_id").isString()) {
                suspected.add(item);
            }
        }
        suspected.sort(Comparator.comparingDouble((JsonNode n) -> -n.path("max_height_diff_m").asDouble(0.0))
                .thenComparing(n -> n.path("zone_id").asString("")));
        ArrayNode out = mapper.createArrayNode();
        if (suspected.isEmpty()) {
            return out;
        }
        Map<String, ObjectNode> current = new HashMap<>();
        jdbc.query("""
                SELECT DISTINCT ON (zone_id) zone_id, name, zone_kind, ST_AsGeoJSON(polygon) AS polygon, z_min_m, z_max_m
                FROM zone
                WHERE site_id = :site AND valid_to IS NULL
                ORDER BY zone_id, zone_version DESC
                """, new MapSqlParameterSource("site", siteId), rs -> {
            ObjectNode z = mapper.createObjectNode();
            z.put("zone_id", rs.getString("zone_id"));
            String name = rs.getString("name");
            if (name != null && !name.isBlank() && name.length() <= 160) {
                z.put("name", name);
            }
            String kind = rs.getString("zone_kind");
            if (kind != null && !kind.isBlank() && kind.length() <= 64) {
                z.put("zone_kind", kind);
            }
            ArrayNode poly = z.putArray("polygon_xy");
            Versioned.polygonXy(mapper.readTree(rs.getString("polygon")), poly);
            putNullableNumber(z, "z_min_m", Versioned.real(rs, "z_min_m"));
            putNullableNumber(z, "z_max_m", Versioned.real(rs, "z_max_m"));
            current.put(rs.getString("zone_id"), z);
        });
        for (JsonNode item : suspected) {
            if (out.size() >= MAX_ZONES) {
                break;
            }
            ObjectNode z = current.get(item.get("zone_id").asString());
            int vertices = z == null ? 0 : z.path("polygon_xy").size();
            if (z == null || vertices < 3 || vertices > MAX_POLYGON || z.path("zone_id").asString().length() > 100) {
                continue;  // 그새 지워졌거나 계약 밖 모양이면 의견을 묻지 않는다
            }
            ObjectNode entry = z.deepCopy();
            copyNumber(item, entry, "changed_area_ratio");
            copyNumber(item, entry, "max_height_diff_m");
            JsonNode xy = item.get("max_diff_xy");
            if (xy != null && xy.isArray() && xy.size() == 2 && xy.get(0).isNumber() && xy.get(1).isNumber()) {
                entry.set("max_diff_xy", xy.deepCopy());
            }
            out.add(entry);
        }
        return out;
    }

    /**
     * 이전 관제 Scene 을 만든 세팅 세션(그 Scene 을 등록한 세션, 보강 다시 굽기로 만든 Scene 이면 그 세션)의 보정 참조와 CAMERA_FRAME.
     * 보정 참조나 사진이 없으면 아무것도 싣지 않는다(워커가 NO_PREVIOUS_FRAMES 로 답한다).
     */
    private void addPreviousSetup(UUID setupSessionId, String siteId, String previousScene, ObjectNode input) {
        record Previous(UUID sessionId, String calibrationVersion, String previewSceneVersion, String calibrationRef,
                String calibrationHash) { }
        Previous prev = jdbc.query("""
                SELECT s.setup_session_id, s.calibration_version, s.preview_scene_version,
                       c.calibration_ref, c.calibration_hash
                FROM (
                    SELECT a.setup_session_id, a.registered_at AS at FROM setup_session_artifact a
                    WHERE a.kind IN ('SCENE', 'PREVIEW_SCENE') AND a.version = :scene
                    UNION ALL
                    SELECT r.setup_session_id, r.requested_at AS at FROM setup_supplement_rebake r
                    WHERE r.result_scene_version = :scene
                ) found
                JOIN setup_session s ON s.setup_session_id = found.setup_session_id AND s.site_id = :site
                LEFT JOIN calibration_version c ON c.calibration_version = s.calibration_version AND c.site_id = s.site_id
                WHERE s.setup_session_id <> :current
                ORDER BY found.at DESC
                LIMIT 1
                """, new MapSqlParameterSource("scene", previousScene).addValue("site", siteId)
                .addValue("current", setupSessionId), (rs, i) -> new Previous(rs.getObject("setup_session_id", UUID.class),
                rs.getString("calibration_version"), rs.getString("preview_scene_version"), rs.getString("calibration_ref"),
                rs.getString("calibration_hash"))).stream().findFirst().orElse(null);
        if (prev == null || prev.calibrationVersion() == null || prev.calibrationRef() == null
                || !S3_URI.matcher(prev.calibrationRef()).matches()) {
            log.info("세팅 세션 {}: 이전 Scene {} 의 세팅 사진·보정을 찾지 못해 구역 의견은 NO_OPINION 이 된다", setupSessionId, previousScene);
            return;
        }
        ArrayNode frames = sessionFrames(prev.sessionId(), prev.previewSceneVersion());
        if (frames.isEmpty()) {
            log.info("세팅 세션 {}: 이전 세팅 세션 {} 에 CAMERA_FRAME 이 없다", setupSessionId, prev.sessionId());
            return;
        }
        input.put("previous_setup_session_id", prev.sessionId().toString());
        input.put("previous_calibration_version", prev.calibrationVersion());
        input.put("previous_calibration_ref", prev.calibrationRef());
        if (prev.calibrationHash() != null && CALIBRATION_HASH.matcher(prev.calibrationHash()).matches()) {
            input.put("previous_calibration_hash", prev.calibrationHash());
        }
        input.set("previous_frames", frames);
    }

    /** 세션의 카메라별 대표 프레임. 미리보기를 등록한 대여의 것을 쓰고, 없으면 가장 최근 등록분(SetupService 초안 입력과 같은 규칙) */
    private ArrayNode sessionFrames(UUID sessionId, String previewSceneVersion) {
        List<Map<String, Object>> rows = jdbc.queryForList("""
                SELECT f.camera_id, f.object_uri, f.face_policy
                FROM setup_session_artifact f
                WHERE f.setup_session_id = :sessionId AND f.kind = 'CAMERA_FRAME'
                  AND f.lease_id = COALESCE((SELECT p.lease_id FROM setup_session_artifact p
                                             WHERE p.setup_session_id = f.setup_session_id AND p.kind = 'PREVIEW_SCENE'
                                               AND p.version = :previewScene
                                             ORDER BY p.registered_at DESC LIMIT 1), f.lease_id)
                ORDER BY f.camera_id, f.registered_at DESC
                """, new MapSqlParameterSource("sessionId", sessionId).addValue("previewScene", previewSceneVersion));
        ArrayNode frames = mapper.createArrayNode();
        Set<String> seen = new HashSet<>();
        for (Map<String, Object> row : rows) {
            String cameraId = (String) row.get("camera_id");
            String uri = (String) row.get("object_uri");
            String facePolicy = (String) row.get("face_policy");
            if (frames.size() >= MAX_FRAMES || cameraId == null || !CAMERA.matcher(cameraId).matches() || uri == null
                    || !S3_URI.matcher(uri).matches() || !("BLURRED".equals(facePolicy) || "EXCLUDED".equals(facePolicy))
                    || !seen.add(cameraId)) {
                continue;
            }
            frames.addObject().put("camera_id", cameraId).put("frame_uri", uri).put("face_policy", facePolicy);
        }
        return frames;
    }

    // ------------------------------------------------------------------------------------------------ 결과 저장

    /** 콜백 결과의 zone_opinions 를 추가한다. 표 제약을 어기는 항목만 뺀다(나머지 의견·초안은 살린다) */
    void store(UUID draftRunId, UUID setupSessionId, String jobId, JsonNode result, OffsetDateTime now) {
        JsonNode opinions = result == null ? null : result.get("zone_opinions");
        if (opinions == null || !opinions.isArray() || opinions.isEmpty()) {
            return;
        }
        JsonNode input = jdbc.queryForList("SELECT input::text FROM analysis_job WHERE job_id = :job",
                new MapSqlParameterSource("job", jobId), String.class).stream().findFirst()
                .map(mapper::readTree).orElse(mapper.createObjectNode());
        Map<String, String> before = frameUris(input.get("previous_frames"));
        Map<String, String> after = frameUris(input.get("frames"));
        String previousSession = input.path("previous_setup_session_id").asString(null);
        Set<String> seen = new HashSet<>();
        for (int i = 0; i < opinions.size() && i < MAX_ZONES; i++) {
            JsonNode o = opinions.get(i);
            String problem = problem(o);
            if (problem != null || !seen.add(o.get("zone_id").asString())) {
                log.warn("vlm_zone_draft 작업 {} 의 구역 의견 {} 를 뺀다: {}", jobId, i, problem == null ? "zone_id 중복" : problem);
                continue;
            }
            String camera = o.path("camera_id").isString() ? o.get("camera_id").asString() : null;
            jdbc.update("""
                    INSERT INTO setup_zone_opinion (
                        opinion_id, draft_run_id, setup_session_id, job_id, zone_id, status, reason, recommended_action,
                        rationale, confidence, camera_id, before_crop_box, after_crop_box, before_frame_uri, after_frame_uri,
                        previous_setup_session_id, model_version, prompt_version, created_at
                    ) VALUES (
                        :id, :runId, :sessionId, :jobId, :zoneId, :status, :reason, :action,
                        :rationale, :confidence, :camera, CAST(:beforeBox AS jsonb), CAST(:afterBox AS jsonb), :beforeUri, :afterUri,
                        :previousSession, :model, :prompt, :now
                    )
                    ON CONFLICT (draft_run_id, zone_id) DO NOTHING
                    """, new MapSqlParameterSource()
                    .addValue("id", UUID.randomUUID())
                    .addValue("runId", draftRunId)
                    .addValue("sessionId", setupSessionId)
                    .addValue("jobId", jobId)
                    .addValue("zoneId", o.get("zone_id").asString())
                    .addValue("status", o.get("status").asString())
                    .addValue("reason", textOrNull(o, "reason"))
                    .addValue("action", textOrNull(o, "recommended_action"))
                    .addValue("rationale", textOrNull(o, "rationale"))
                    .addValue("confidence", o.path("confidence").isNumber() ? (float) o.get("confidence").doubleValue() : null)
                    .addValue("camera", camera)
                    .addValue("beforeBox", camera == null ? null : mapper.writeValueAsString(o.get("before_crop_box_norm")))
                    .addValue("afterBox", camera == null ? null : mapper.writeValueAsString(o.get("after_crop_box_norm")))
                    .addValue("beforeUri", camera == null ? null : before.get(camera))
                    .addValue("afterUri", camera == null ? null : after.get(camera))
                    .addValue("previousSession", previousSession == null ? null : parseUuid(previousSession))
                    .addValue("model", textOrNull(o, "model_version"))
                    .addValue("prompt", o.get("prompt_version").asString())
                    .addValue("now", now));
        }
    }

    /** V66 표 제약과 계약 불변식을 먼저 본다. 어기면 이유, 맞으면 null */
    static String problem(JsonNode o) {
        if (o == null || !o.isObject()) {
            return "객체가 아님";
        }
        if (!textWithin(o, "zone_id", 100) || !textWithin(o, "prompt_version", 160)) {
            return "zone_id·prompt_version 누락 또는 너무 김";
        }
        String status = o.path("status").asString("");
        String camera = o.path("camera_id").isString() ? o.get("camera_id").asString() : null;
        if (camera != null && !CAMERA.matcher(camera).matches()) {
            return "camera_id 형식";
        }
        if (camera != null ? !(box(o.get("before_crop_box_norm")) && box(o.get("after_crop_box_norm")))
                : !(isNull(o.get("before_crop_box_norm")) && isNull(o.get("after_crop_box_norm")))) {
            return "크롭 박스가 camera_id 와 맞지 않음";
        }
        JsonNode conf = o.get("confidence");
        if (!isNull(conf) && !(conf.isNumber() && conf.doubleValue() >= 0.0 && conf.doubleValue() <= 1.0)) {
            return "confidence 범위";
        }
        if ("OPINION".equals(status)) {
            boolean ok = isNull(o.get("reason")) && ACTIONS.contains(o.path("recommended_action").asString(""))
                    && textWithin(o, "rationale", 200) && !o.get("rationale").asString().isBlank() && camera != null
                    && textWithin(o, "model_version", 160);
            return ok ? null : "OPINION 모양";
        }
        if ("NO_OPINION".equals(status)) {
            boolean ok = REASONS.contains(o.path("reason").asString("")) && isNull(o.get("recommended_action"))
                    && isNull(o.get("rationale")) && isNull(conf) && isNull(o.get("model_version"));
            return ok ? null : "NO_OPINION 모양";
        }
        return "status 값이 목록 밖";
    }

    // ------------------------------------------------------------------------------------------------ 보기

    /**
     * 구역 계획 응답에 붙일 구역별 ai_opinion. 이 세션의 지금 미리보기로 낸 초안 작업의 의견만 본다(다시 시도로 미리보기가 바뀌면 이전 것은
     * 보이지 않는다). 그 작업이 아직 대기 중이고 입력에 실린 구역이면 {@code {status: PENDING}}.
     */
    public Map<String, JsonNode> forSession(UUID setupSessionId) {
        Map<String, JsonNode> out = new LinkedHashMap<>();
        record Run(UUID runId, String status, String suspectedZones) { }
        Run run = jdbc.query("""
                SELECT r.draft_run_id, r.status, (j.input -> 'suspected_zones')::text AS suspected
                FROM setup_zone_draft_run r
                JOIN setup_session s ON s.setup_session_id = r.setup_session_id
                    AND r.preview_scene_version = s.preview_scene_version
                LEFT JOIN analysis_job j ON j.job_id = r.job_id
                WHERE r.setup_session_id = :id
                """, new MapSqlParameterSource("id", setupSessionId), (rs, i) -> new Run(
                rs.getObject("draft_run_id", UUID.class), rs.getString("status"), rs.getString("suspected")))
                .stream().findFirst().orElse(null);
        if (run == null || run.suspectedZones() == null) {
            return out;
        }
        if ("QUEUED".equals(run.status())) {
            for (JsonNode z : mapper.readTree(run.suspectedZones())) {
                if (z.path("zone_id").isString()) {
                    out.put(z.get("zone_id").asString(), mapper.createObjectNode().put("status", "PENDING"));
                }
            }
            return out;
        }
        List<Map<String, Object>> rows = jdbc.queryForList("""
                SELECT zone_id, status, reason, recommended_action, rationale, confidence, camera_id,
                       before_crop_box::text AS before_box, after_crop_box::text AS after_box,
                       before_frame_uri, after_frame_uri, model_version, prompt_version, created_at
                FROM setup_zone_opinion
                WHERE draft_run_id = :run
                ORDER BY zone_id
                """, new MapSqlParameterSource("run", run.runId()));
        for (Map<String, Object> row : rows) {
            ObjectNode n = mapper.createObjectNode();
            n.put("status", (String) row.get("status"));
            n.put("reason", (String) row.get("reason"));
            n.put("recommended_action", (String) row.get("recommended_action"));
            n.put("rationale", (String) row.get("rationale"));
            if (row.get("confidence") instanceof Number c) {
                n.put("confidence", Math.round(c.doubleValue() * 1000.0) / 1000.0);
            } else {
                n.putNull("confidence");
            }
            n.put("camera_id", (String) row.get("camera_id"));
            n.put("model_version", (String) row.get("model_version"));
            n.put("prompt_version", (String) row.get("prompt_version"));
            Instant expires = null;
            Signed before = sign((String) row.get("before_frame_uri"));
            Signed after = sign((String) row.get("after_frame_uri"));
            n.set("before", photo(before, (String) row.get("before_box")));
            n.set("after", photo(after, (String) row.get("after_box")));
            for (Signed s : List.of(before, after)) {
                if (s.expiresAt() != null && (expires == null || s.expiresAt().isBefore(expires))) {
                    expires = s.expiresAt();
                }
            }
            if (expires == null) {
                n.putNull("image_expires_at_ms");
            } else {
                n.put("image_expires_at_ms", expires.toEpochMilli());
            }
            if (row.get("created_at") instanceof java.sql.Timestamp t) {
                n.put("created_at", t.getTime());
            } else if (row.get("created_at") instanceof OffsetDateTime t) {
                n.put("created_at", t.toInstant().toEpochMilli());
            }
            out.put((String) row.get("zone_id"), n);
        }
        return out;
    }

    private ObjectNode photo(Signed signed, String boxJson) {
        ObjectNode p = mapper.createObjectNode();
        p.put("image_url", signed.url());
        p.set("crop_box_norm", boxJson == null ? mapper.nullNode() : mapper.readTree(boxJson));
        return p;
    }

    private record Signed(String url, Instant expiresAt) { }

    /** 우리 버킷 s3:// 주소면 서명 GET 주소(SceneCameraImageService 와 같은 서명 방식). 저장소가 없거나 실패하면 주소 없음 */
    private Signed sign(String uri) {
        if (uri == null || !storage.configured()) {
            return new Signed(null, null);
        }
        String prefix = storage.uri("");
        if (!uri.startsWith(prefix) || uri.length() == prefix.length()) {
            return new Signed(null, null);
        }
        try {
            SignedRequest request = storage.signGet(uri.substring(prefix.length()));
            return new Signed(request.url(), request.expiresAt());
        } catch (BusinessException e) {
            log.warn("구역 의견 사진 서명 주소를 만들지 못했다: {}", e.getErrorCode());
            return new Signed(null, null);
        }
    }

    // ------------------------------------------------------------------------------------------------ 도움

    private static Map<String, String> frameUris(JsonNode frames) {
        Map<String, String> out = new HashMap<>();
        if (frames != null && frames.isArray()) {
            for (JsonNode f : frames) {
                String cam = f.path("camera_id").asString(null);
                String uri = f.path("frame_uri").asString(null);
                if (cam != null && uri != null && S3_URI.matcher(uri).matches()) {
                    out.putIfAbsent(cam, uri);
                }
            }
        }
        return out;
    }

    private static boolean box(JsonNode n) {
        if (n == null || !n.isArray() || n.size() != 4) {
            return false;
        }
        for (JsonNode v : n) {
            if (!v.isNumber() || !(v.doubleValue() >= 0.0 && v.doubleValue() <= 1.0)) {
                return false;
            }
        }
        return n.get(0).doubleValue() < n.get(2).doubleValue() && n.get(1).doubleValue() < n.get(3).doubleValue();
    }

    private static boolean isNull(JsonNode n) {
        return n == null || n.isNull();
    }

    private static boolean textWithin(JsonNode o, String field, int max) {
        JsonNode n = o.get(field);
        return n != null && n.isString() && !n.asString().isEmpty() && n.asString().length() <= max;
    }

    private static String textOrNull(JsonNode o, String field) {
        JsonNode n = o.get(field);
        return n != null && n.isString() ? n.asString() : null;
    }

    private static void putNullableNumber(ObjectNode node, String field, Double value) {
        if (value == null) {
            node.putNull(field);
        } else {
            node.put(field, value);
        }
    }

    private static void copyNumber(JsonNode from, ObjectNode to, String field) {
        JsonNode n = from.get(field);
        if (n != null && n.isNumber()) {
            to.put(field, n.doubleValue());
        }
    }

    private static UUID parseUuid(String value) {
        try {
            return UUID.fromString(value);
        } catch (IllegalArgumentException e) {
            return null;
        }
    }
}
