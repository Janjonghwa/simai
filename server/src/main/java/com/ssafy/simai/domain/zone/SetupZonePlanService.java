package com.ssafy.simai.domain.zone;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.geometry.StaticObjectRepository;
import com.ssafy.simai.domain.geometry.StaticObjectRequest;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
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
     */
    public record Item(
            @JsonProperty("zone_id") String zoneId,
            @JsonProperty("source") String source,
            @JsonProperty("candidate_id") String candidateId,
            @JsonProperty("zone") ZoneRequest zone,
            @JsonProperty("surface") StaticObjectRequest surface) {
    }

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final SiteLookup sites;
    private final Clock clock;
    private final TransactionTemplate transaction;
    private final ZoneRepository zones;
    private final StaticObjectRepository surfaces;

    public SetupZonePlanService(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, SiteLookup sites, Clock clock,
            TransactionTemplate transaction, ZoneRepository zones, StaticObjectRepository surfaces) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.sites = sites;
        this.clock = clock;
        this.transaction = transaction;
        this.zones = zones;
        this.surfaces = surfaces;
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
        record Row(String items, int revision, Timestamp updatedAt) { }
        List<Row> rows = jdbc.query("""
                SELECT items::text, revision, updated_at FROM setup_session_zone_plan WHERE setup_session_id = :id
                """, new MapSqlParameterSource("id", id), (rs, i) -> new Row(rs.getString(1), rs.getInt(2), rs.getTimestamp(3)));
        if (rows.isEmpty()) {
            out.putArray("items");
            out.put("revision", 0);
            out.putNull("updated_at");
        } else {
            out.set("items", mapper.readTree(rows.getFirst().items()));
            out.put("revision", rows.getFirst().revision());
            out.put("updated_at", rows.getFirst().updatedAt().getTime());
        }
        return out;
    }

    /**
     * 이 현장의 세션인지 본다. forWrite 면 고칠 수 있는 때인지도 본다: 세션이 실패(취소 포함)가 아니고,
     * 작업이 PLANNED 이고, 세팅 완료(setup_completed_at) 전. 세션 행을 잠가 적용과 줄을 세운다.
     */
    private void requireSession(UUID id, boolean forWrite) {
        record S(String status, String workStatus, boolean completed, boolean active) { }
        // 세션·작업 행을 잠근다. 세팅 완료(같은 행을 잠그고 setup_completed_at 을 쓴다)와 줄을 세워 완료 뒤 저장을 막는다
        List<S> rows = jdbc.query("""
                SELECT s.status, w.status, w.setup_completed_at IS NOT NULL,
                       w.active_setup_session_id IS NOT DISTINCT FROM s.setup_session_id
                FROM setup_session s JOIN work_unit w ON w.work_id = s.work_unit_id
                WHERE s.setup_session_id = :id AND s.site_id = :site
                """ + (forWrite ? " FOR UPDATE OF s, w" : ""),
                new MapSqlParameterSource("id", id).addValue("site", sites.siteId()),
                (rs, i) -> new S(rs.getString(1), rs.getString(2), rs.getBoolean(3), rs.getBoolean(4)));
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Setup.SETUP_SESSION_NOT_FOUND);
        }
        S s = rows.getFirst();
        // 다시 시도로 새 세션이 생기면 예전 세션 목록은 적용되지 않으므로 고치지 못하게 한다
        if (forWrite && ("FAILED".equals(s.status()) || !"PLANNED".equals(s.workStatus()) || s.completed() || !s.active())) {
            throw new BusinessException(ErrorCode.Config.SETUP_ZONES_CLOSED);
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
            out.add(new Item(zoneId, item.source(), candidate, item.zone(), item.surface()));
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
