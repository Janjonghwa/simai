package com.ssafy.simai.domain.zone;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.config.Versioned;
import com.ssafy.simai.domain.geometry.StaticObjectRepository;
import com.ssafy.simai.domain.geometry.StaticObjectRequest;
import com.ssafy.simai.domain.health.ReplayTestSwitch;
import com.ssafy.simai.domain.setup.SetupCameraCheck;
import com.ssafy.simai.domain.setup.SetupZoneHeightChanges;
import com.ssafy.simai.domain.setup.SetupZoneOpinions;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 중 구역 목록(setup_session_zone_plan, S15P31A101-612, docs/design/setup-session-zones.md).
 * 세팅 화면은 AI 위험구역 초안을 처음부터 구역으로 깔고 고치고 더하고 지운 뒤, 목록을 통째로 저장한다.
 * 관제 구역(zone)에는 쓰지 않는다. 세팅 완료(C17) 때 {@link SetupZoneApplier} 가 한 번에 적용한다.
 * 초안을 구역으로 바꾸는 규칙은 화면(draftZone.ts)이 한다. 서버는 모양·값을 검사하고 저장만 한다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SetupZonePlanService {

    private static final Logger log = LoggerFactory.getLogger(SetupZonePlanService.class);

    static final int MAX_ITEMS = 100;
    static final int MAX_ID = 100;

    /**
     * 목록 항목. zone_id 가 있으면 지금 관제 구역을 이어 쓴다(zone 이 null 이면 그대로, 있으면 새 버전).
     * zone_id 가 없으면 새 구역(zone 필수). surface 는 그 구역의 작업면 새 값(null 이면 그대로·없음).
     * needs_review(S15P31A101-629): 새 모델 + World 이어짐 세팅이 지금 관제 구역으로 채운 항목은 사람이 확인하기 전까지 true 다.
     * 없으면 false(확인된 것). 저장할 때 이어 쓰는 구역을 고치지 않은 항목만 true 를 남긴다(고치면 확인한 것으로 본다).
     */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record Item(
            @JsonProperty("zone_id") String zoneId,
            @JsonProperty("source") String source,
            @JsonProperty("candidate_id") String candidateId,
            @JsonProperty("zone") ZoneRequest zone,
            @JsonProperty("surface") StaticObjectRequest surface,
            @JsonProperty("needs_review") Boolean needsReview) {

        /** 확인이 필요한 항목인지(null 은 false) */
        public boolean pendingReview() {
            return Boolean.TRUE.equals(needsReview);
        }
    }

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final SiteLookup sites;
    private final Clock clock;
    private final TransactionTemplate transaction;
    private final ZoneRepository zones;
    private final StaticObjectRepository surfaces;
    private final SetupZoneOpinions opinions;
    private final SetupZoneReuseSeed reuseSeed;
    private final ReplayTestSwitch replayTest;

    public SetupZonePlanService(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, SiteLookup sites, Clock clock,
            TransactionTemplate transaction, ZoneRepository zones, StaticObjectRepository surfaces,
            SetupZoneOpinions opinions, SetupZoneReuseSeed reuseSeed, ReplayTestSwitch replayTest) {
        this.reuseSeed = reuseSeed;
        this.replayTest = replayTest;
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.sites = sites;
        this.clock = clock;
        this.transaction = transaction;
        this.zones = zones;
        this.surfaces = surfaces;
        this.opinions = opinions;
    }

    /** 응답: {setup_session_id, items, revision, updated_at}. 저장한 적이 없으면 빈 목록·revision 0 */
    public ObjectNode get(String setupSessionId) {
        UUID id = parseId(setupSessionId);
        requireSession(id, false);
        return view(id);
    }

    /** 목록을 통째로 바꾼다. revision 이 마지막으로 읽은 값과 다르면 409 SETUP_ZONE_PLAN_CONFLICT */
    public ObjectNode put(String setupSessionId, JsonNode items, Integer revision, long userId) {
        UUID id = parseId(setupSessionId);
        if (revision == null || revision < 0) {
            throw invalid();
        }
        List<Item> parsed = parse(items);
        // 값 검사(다각형·높이 등)는 적용 때와 같은 규칙으로 저장 전에 한다. 완료 때 처음 실패하지 않게
        for (Item item : parsed) {
            if (item.zone() != null) zones.validate(item.zone());
            if (item.surface() != null) surfaces.validate(item.surface());
        }
        String json = mapper.writeValueAsString(parsed);
        transaction.executeWithoutResult(s -> {
            requireSession(id, true);
            MapSqlParameterSource p = new MapSqlParameterSource("id", id).addValue("items", json).addValue("user", userId)
                    .addValue("now", OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC));
            List<Integer> current = jdbc.queryForList(
                    "SELECT revision FROM setup_session_zone_plan WHERE setup_session_id = :id FOR UPDATE", p, Integer.class);
            int stored = current.isEmpty() ? 0 : current.getFirst();
            if (stored != revision) {
                throw new BusinessException(ErrorCode.Config.SETUP_ZONE_PLAN_CONFLICT);
            }
            if (current.isEmpty()) {
                jdbc.update("""
                        INSERT INTO setup_session_zone_plan (setup_session_id, items, revision, updated_by, updated_at)
                        VALUES (:id, CAST(:items AS jsonb), 1, :user, :now)
                        """, p);
            } else {
                jdbc.update("""
                        UPDATE setup_session_zone_plan SET items = CAST(:items AS jsonb), revision = revision + 1,
                            updated_by = :user, updated_at = :now
                        WHERE setup_session_id = :id
                        """, p);
            }
        });
        return view(id);
    }

    /** 세팅 방식: 재사용인지, World 다시 맞추기를 채택했는지, 고른 맵(재사용 원본 세션)과 그 보정 */
    private record Mode(boolean reuse, boolean anchorAccepted, UUID reuseSource, String sourceCalibration) {
    }

    private Mode mode(MapSqlParameterSource p) {
        return jdbc.queryForObject("""
                SELECT s.mode = 'REUSE', COALESCE((s.world_anchor ->> 'accepted')::boolean, false), s.reuse_source_session_id,
                       src.calibration_version
                FROM setup_session s LEFT JOIN setup_session src ON src.setup_session_id = s.reuse_source_session_id
                WHERE s.setup_session_id = :id
                """, p, (rs, i) -> new Mode(rs.getBoolean(1), rs.getBoolean(2), rs.getObject(3, UUID.class), rs.getString(4)));
    }

    private MapSqlParameterSource writeParams(UUID id, long userId) {
        return new MapSqlParameterSource("id", id).addValue("site", sites.siteId()).addValue("user", userId)
                .addValue("now", OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC));
    }

    /**
     * 처음 목록 채우기(S15P31A101-629). 한 번도 저장하지 않은 목록(revision 0)만 채우고, 이미 저장한 목록이면 그대로 돌려준다.
     * 이전 모델 재사용(REUSE)만 채운다: 지금 관제 구역(zone_id 를 잇고 본문은 비운다)으로 확인 표시 없이, 고른 맵의 묶음을 그대로 쓰는
     * 재사용(S15P31A101-714)은 그 맵으로 마지막에 발행한 구역으로({@link SetupZoneReuseSeed}).
     * 새 모델(NEW)은 World 이어짐이어도 채우지 않는다(S15P31A101-758): 구역 단계는 AI 초안만으로 시작하고, 이전 구역은 관리자가
     * 「이전 구역 불러오기」({@link #importPrevious})를 눌렀을 때만 확인 필요로 이어 온다.
     */
    public ObjectNode seed(String setupSessionId, long userId) {
        UUID id = parseId(setupSessionId);
        transaction.executeWithoutResult(s -> {
            requireSession(id, true);
            MapSqlParameterSource p = writeParams(id, userId);
            Mode mode = mode(p);
            if (mode == null || !mode.reuse()) {
                return;
            }
            if (!jdbc.queryForList("SELECT revision FROM setup_session_zone_plan WHERE setup_session_id = :id FOR UPDATE",
                    p, Integer.class).isEmpty()) {
                return;
            }
            List<Item> items = mode.reuseSource() != null
                    ? reuseSeed.items(sites.siteId(), mode.reuseSource(), mode.sourceCalibration())
                    : jdbc.query("""
                    SELECT DISTINCT zone_id FROM zone WHERE site_id = :site AND valid_to IS NULL ORDER BY zone_id
                    """, p, (rs, i) -> new Item(rs.getString("zone_id"), "MANUAL", null, null, null, false));
            if (items.size() > MAX_ITEMS) {
                // 상한을 넘는 현장은 없지만, 넘으면 반쯤 채우지 않고 사람이 그리게 둔다
                log.warn("Setup zone plan seed skipped: {} current zones exceed {}", items.size(), MAX_ITEMS);
                return;
            }
            p.addValue("items", mapper.writeValueAsString(items));
            jdbc.update("""
                    INSERT INTO setup_session_zone_plan (setup_session_id, items, revision, updated_by, updated_at)
                    VALUES (:id, CAST(:items AS jsonb), 1, :user, :now)
                    ON CONFLICT (setup_session_id) DO NOTHING
                    """, p);
        });
        return view(id);
    }

    /**
     * 「이전 구역 불러오기」(S15P31A101-758). 새 모델 + World 이어짐(world_anchor.accepted) 세팅에서 지금 관제 구역을 목록에 더한다.
     * 저장 여부(revision)와 상관없이 지금 목록에 덧붙이고(목록 행을 잠근다, revision + 1), 확인 필요(needs_review=true)로 둔다.
     * 레이아웃이 만든 구역은 확인 표시 없이 둔다(-629 와 같다). 이미 목록에 있는 구역은 다시 넣지 않는다.
     * 목록의 AI 초안 구역(source AI, 본문 있음)과 같은 자리({@link SetupZoneOverlap#sameSpot})인 구역은 넣지 않고 응답
     * import_skipped 에 이름을 싣는다(같은 개구부를 둘 만들지 않는다). World 가 끊긴 새 모델은 409 SETUP_ZONE_WORLD_MISMATCH,
     * 재사용은 처음 채우기({@link #seed})와 같다. 응답은 GET 과 같고 import_skipped(문자열 배열)를 더한다.
     */
    public ObjectNode importPrevious(String setupSessionId, long userId) {
        UUID id = parseId(setupSessionId);
        List<String> skipped = new ArrayList<>();
        Boolean reuse = transaction.execute(s -> {
            requireSession(id, true);
            MapSqlParameterSource p = writeParams(id, userId);
            Mode mode = mode(p);
            if (mode != null && mode.reuse()) {
                return true;
            }
            if (mode == null || !mode.anchorAccepted()) {
                throw new BusinessException(ErrorCode.Config.SETUP_ZONE_WORLD_MISMATCH);
            }
            List<Integer> revision = jdbc.queryForList(
                    "SELECT revision FROM setup_session_zone_plan WHERE setup_session_id = :id FOR UPDATE", p, Integer.class);
            List<Item> current = new ArrayList<>(lockItems(id));
            Set<String> listed = new HashSet<>();
            List<SetupZoneOverlap.Footprint> drafts = new ArrayList<>();
            for (Item item : current) {
                if (item.zoneId() != null) listed.add(item.zoneId());
                if ("AI".equals(item.source()) && item.zone() != null) drafts.add(SetupZoneOverlap.Footprint.of(item.zone()));
            }
            List<Item> added = new ArrayList<>();
            jdbc.query("""
                    SELECT zone_id, name, layout_item_id IS NOT NULL AS layout, ST_AsGeoJSON(polygon) AS polygon, z_min_m, z_max_m
                    FROM zone WHERE site_id = :site AND valid_to IS NULL ORDER BY zone_id
                    """, p, rs -> {
                String zoneId = rs.getString("zone_id");
                if (!listed.add(zoneId)) {
                    return;
                }
                List<double[]> polygon = new ArrayList<>();
                Versioned.polygonXy(mapper.readTree(rs.getString("polygon")), mapper.createArrayNode())
                        .forEach(point -> polygon.add(new double[] {point.get(0).doubleValue(), point.get(1).doubleValue()}));
                SetupZoneOverlap.Footprint carried = new SetupZoneOverlap.Footprint(polygon, Versioned.real(rs, "z_min_m"),
                        Versioned.real(rs, "z_max_m"));
                if (drafts.stream().anyMatch(draft -> SetupZoneOverlap.sameSpot(carried, draft))) {
                    skipped.add(rs.getString("name"));
                    return;
                }
                added.add(new Item(zoneId, "MANUAL", null, null, null, !rs.getBoolean("layout")));
            });
            if (added.isEmpty()) {
                return false;
            }
            current.addAll(added);
            if (current.size() > MAX_ITEMS) {
                // 반쯤 더하지 않는다. 사람이 목록을 줄이고 다시 불러오게 한다
                log.warn("Setup zone plan import rejected: {} items exceed {}", current.size(), MAX_ITEMS);
                throw invalid();
            }
            p.addValue("items", mapper.writeValueAsString(current));
            if (revision.isEmpty()) {
                jdbc.update("""
                        INSERT INTO setup_session_zone_plan (setup_session_id, items, revision, updated_by, updated_at)
                        VALUES (:id, CAST(:items AS jsonb), 1, :user, :now)
                        """, p);
            } else {
                jdbc.update("""
                        UPDATE setup_session_zone_plan SET items = CAST(:items AS jsonb), revision = revision + 1,
                            updated_by = :user, updated_at = :now
                        WHERE setup_session_id = :id
                        """, p);
            }
            return false;
        });
        if (Boolean.TRUE.equals(reuse)) {
            return seed(setupSessionId, userId);
        }
        ObjectNode out = view(id);
        ArrayNode names = out.putArray("import_skipped");
        skipped.forEach(names::add);
        return out;
    }

    /** 적용 서비스용. 부르는 쪽 트랜잭션에서 목록을 잠그고 읽는다. 저장한 적이 없으면 빈 목록 */
    List<Item> lockItems(UUID setupSessionId) {
        List<String> rows = jdbc.queryForList(
                "SELECT items::text FROM setup_session_zone_plan WHERE setup_session_id = :id FOR UPDATE",
                new MapSqlParameterSource("id", setupSessionId), String.class);
        return rows.isEmpty() ? List.of() : parse(mapper.readTree(rows.getFirst()));
    }

    private ObjectNode view(UUID id) {
        ObjectNode out = mapper.createObjectNode();
        out.put("setup_session_id", id.toString());
        record SessionHeightState(boolean reuse, Boolean anchorAccepted, String zoneHeightChanges,
                boolean hasPreviousScene) { }
        SessionHeightState heightState = jdbc.query("""
                SELECT s.mode = 'REUSE' AS reuse,
                       (s.world_anchor ->> 'accepted')::boolean AS anchor_accepted,
                       s.zone_height_changes::text AS zone_height_changes,
                       EXISTS (
                           SELECT 1 FROM analysis_job j
                           WHERE j.job_id = s.job_id AND (j.input -> 'previous_scene') IS NOT NULL
                       ) OR (s.zone_height_changes ->> 'previous_scene_version') IS NOT NULL AS has_previous_scene
                FROM setup_session s
                WHERE s.setup_session_id = :id
                """, new MapSqlParameterSource("id", id), (rs, i) -> {
            boolean anchor = rs.getBoolean("anchor_accepted");
            Boolean anchorAccepted = rs.wasNull() ? null : anchor;
            return new SessionHeightState(rs.getBoolean("reuse"), anchorAccepted,
                    rs.getString("zone_height_changes"), rs.getBoolean("has_previous_scene"));
        }).stream().findFirst().orElse(new SessionHeightState(false, null, null, false));
        Map<String, JsonNode> heightByZone = SetupZoneHeightChanges.indexByZoneId(mapper, heightState.zoneHeightChanges());
        // 변경 의심 구역 VLM 의견과 전·후 사진(S15P31A101-631). 조회·저장 응답에만 붙고 저장 요청 본문으로는 받지 않는다
        Map<String, JsonNode> opinionByZone = opinions.forSession(id);

        record Row(String items, int revision, Timestamp updatedAt) { }
        List<Row> rows = jdbc.query("""
                SELECT items::text, revision, updated_at FROM setup_session_zone_plan WHERE setup_session_id = :id
                """, new MapSqlParameterSource("id", id), (rs, i) -> new Row(rs.getString(1), rs.getInt(2), rs.getTimestamp(3)));
        if (rows.isEmpty()) {
            out.putArray("items");
            out.put("revision", 0);
            out.putNull("updated_at");
        } else {
            JsonNode itemsNode = mapper.readTree(rows.getFirst().items());
            if (itemsNode.isArray()) {
                for (JsonNode item : itemsNode) {
                    if (item instanceof ObjectNode itemObj) {
                        JsonNode zoneIdNode = itemObj.get("zone_id");
                        String zoneId = zoneIdNode != null && zoneIdNode.isString() ? zoneIdNode.asString() : null;
                        itemObj.set("height_change", SetupZoneHeightChanges.resolveForZone(
                                mapper, zoneId, heightByZone, heightState.reuse(),
                                heightState.anchorAccepted(), heightState.hasPreviousScene()));
                        JsonNode opinion = zoneId == null ? null : opinionByZone.get(zoneId);
                        if (opinion != null) {
                            itemObj.set("ai_opinion", opinion);
                        }
                    }
                }
            }
            out.set("items", itemsNode);
            out.put("revision", rows.getFirst().revision());
            out.put("updated_at", rows.getFirst().updatedAt().getTime());
        }
        out.set("zone_height_changes", heightState.zoneHeightChanges() == null
                ? mapper.nullNode() : mapper.readTree(heightState.zoneHeightChanges()));
        return out;
    }

    /**
     * 이 현장의 세션인지 본다. forWrite 면 고칠 수 있는 때인지도 본다: 세션이 실패(취소 포함)가 아니고,
     * 작업이 PLANNED 이고, 세팅 완료(setup_completed_at) 전. 세션 행을 잠가 적용과 줄을 세운다.
     */
    private void requireSession(UUID id, boolean forWrite) {
        record S(String status, String workStatus, boolean completed, boolean active, String calibration,
                 String checkStatus, String checkCalibration, boolean reuseAsIs) { }
        // 세션·작업 행을 잠근다. 세팅 완료(같은 행을 잠그고 setup_completed_at 을 쓴다)와 줄을 세워 완료 뒤 저장을 막는다
        List<S> rows = jdbc.query("""
                SELECT s.status, w.status, w.setup_completed_at IS NOT NULL,
                       w.active_setup_session_id IS NOT DISTINCT FROM s.setup_session_id,
                       s.calibration_version, s.camera_check ->> 'status', s.camera_check ->> 'calibration_version',
                       s.reuse_source_session_id IS NOT NULL
                FROM setup_session s JOIN work_unit w ON w.work_id = s.work_unit_id
                WHERE s.setup_session_id = :id AND s.site_id = :site
                """ + (forWrite ? " FOR UPDATE OF s, w" : ""),
                new MapSqlParameterSource("id", id).addValue("site", sites.siteId()),
                (rs, i) -> new S(rs.getString(1), rs.getString(2), rs.getBoolean(3), rs.getBoolean(4), rs.getString(5),
                        rs.getString(6), rs.getString(7), rs.getBoolean(8)));
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Setup.SETUP_SESSION_NOT_FOUND);
        }
        S s = rows.getFirst();
        // 다시 시도로 새 세션이 생기면 예전 세션 목록은 적용되지 않으므로 고치지 못하게 한다
        if (forWrite && ("FAILED".equals(s.status()) || !"PLANNED".equals(s.workStatus()) || s.completed() || !s.active())) {
            throw new BusinessException(ErrorCode.Config.SETUP_ZONES_CLOSED);
        }
        // 구역 단계는 카메라 확인을 통과한 보정에서만 연다(S15P31A101-646). 읽기는 막지 않는다.
        // 고른 맵의 묶음을 그대로 쓰는 재사용(-714)은 시작 때 1단계 시점 확인을 통과했고 걷기 확인을 하지 않는다
        // 녹화 재생 시험 건너뜀(SKIPPED_REPLAY, -736)은 스위치가 켜져 있고 노트북 입력이 모두 녹화 파일인 동안만 통과로 본다
        if (forWrite && !s.reuseAsIs() && !SetupCameraCheck.satisfied(s.checkStatus(), s.checkCalibration(), s.calibration(),
                replayTest.cameraCheckSkipAllowed())) {
            throw new BusinessException(ErrorCode.Setup.SETUP_CAMERA_CHECK_NOT_PASSED);
        }
    }

    /** 모양 검사: 배열, 100개 이하, source AI·MANUAL, 새 구역은 zone 필수, zone_id·candidate_id 중복 없음 */
    private List<Item> parse(JsonNode items) {
        if (items == null || !items.isArray() || items.size() > MAX_ITEMS) {
            throw invalid();
        }
        List<Item> out = new ArrayList<>();
        Set<String> zoneIds = new HashSet<>();
        Set<String> candidates = new HashSet<>();
        for (JsonNode node : items) {
            if (!node.isObject()) {
                throw invalid();
            }
            Item item;
            try {
                item = mapper.treeToValue(node, Item.class);
            } catch (RuntimeException e) {
                log.info("Invalid setup zone plan item: {}", e.getMessage());
                throw invalid();
            }
            String zoneId = blank(item.zoneId()) ? null : item.zoneId().strip();
            String candidate = blank(item.candidateId()) ? null : item.candidateId().strip();
            if (!"AI".equals(item.source()) && !"MANUAL".equals(item.source())
                    || (zoneId == null && item.zone() == null)
                    || (zoneId != null && (zoneId.length() > MAX_ID || !zoneIds.add(zoneId)))
                    || (candidate != null && (candidate.length() > MAX_ID || !candidates.add(candidate)))) {
                throw invalid();
            }
            // 확인 필요는 이어 쓰는 구역을 그대로 둔 항목에만 남는다. 새 구역·고친 구역은 사람이 그렸거나 고쳤으니 확인된 것이다
            boolean review = item.pendingReview() && zoneId != null && item.zone() == null && item.surface() == null;
            out.add(new Item(zoneId, item.source(), candidate, item.zone(), item.surface(), review));
        }
        return out;
    }

    private static UUID parseId(String value) {
        try {
            return UUID.fromString(value);
        } catch (IllegalArgumentException | NullPointerException e) {
            throw new BusinessException(ErrorCode.Setup.SETUP_SESSION_NOT_FOUND);
        }
    }

    private static boolean blank(String v) {
        return v == null || v.isBlank();
    }

    private static BusinessException invalid() {
        return new BusinessException(ErrorCode.Config.INVALID_SETUP_ZONE_PLAN);
    }
}
