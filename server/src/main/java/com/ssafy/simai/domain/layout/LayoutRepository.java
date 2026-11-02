package com.ssafy.simai.domain.layout;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 가상 레이아웃(V23 layout_plan, 명세 8.3·11.1). 수정은 같은 행을 바꾸고 revision 을 올린다(db-tables layout_plan).
 * 응답의 layout_version 은 계약 risk_plan_request.layout.layout_version 과 같은 "lv-{revision}" 이다.
 * LIVE 데이터(구역·정적 객체·이벤트)와 섞지 않고, 계산(sim.*)은 TBM 보고서(-33·-101)가 한다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class LayoutRepository {

    static final Set<String> KINDS = Set.of("OPENING", "SCAFFOLD", "EQUIPMENT");
    static final int MAX_NAME = 100;
    static final int MAX_LABEL = 100;
    static final int MAX_ITEM_ID = 64;
    static final int MAX_ITEMS = 200;
    // 관제 앱 YAW_LIMIT_RAD 와 같다(float 로 나타낸 π)
    static final double YAW_LIMIT_RAD = 3.1415927;
    // World 좌표가 비상식적으로 크지 않게(현장은 수백 m 안쪽)
    static final double MAX_ABS_M = 10_000;

    private static final String COLUMNS = """
            layout_id, name, items::text AS items, revision, source, created_at, updated_at
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public LayoutRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 목록은 항목을 빼고 개수만 싣는다(최근에 고친 것부터) */
    public List<ObjectNode> findAll(String siteId) {
        return jdbc.query("""
                SELECT layout_id, name, revision, source, jsonb_array_length(items) AS item_count, created_at, updated_at
                FROM layout_plan WHERE site_id = :site ORDER BY updated_at DESC, layout_id DESC
                """, new MapSqlParameterSource("site", siteId), (rs, i) -> {
            ObjectNode l = summary(rs);
            l.put("item_count", rs.getInt("item_count"));
            return l;
        });
    }

    public Optional<ObjectNode> findOne(String siteId, long layoutId) {
        return jdbc.query("SELECT " + COLUMNS + " FROM layout_plan WHERE site_id = :site AND layout_id = :id",
                new MapSqlParameterSource("site", siteId).addValue("id", layoutId), (rs, i) -> view(rs)).stream().findFirst();
    }

    public long create(String siteId, LayoutRequest request, long userId, Instant now) {
        Checked c = check(request);
        Long id = jdbc.queryForObject("""
                INSERT INTO layout_plan (site_id, name, items, revision, source, created_by, created_at, updated_at)
                VALUES (:site, :name, CAST(:items AS jsonb), 1, 'PLANNED', :user, :now, :now)
                RETURNING layout_id
                """, new MapSqlParameterSource().addValue("site", siteId).addValue("name", c.name())
                .addValue("items", c.items()).addValue("user", userId).addValue("now", utc(now)), Long.class);
        long layoutId = id == null ? 0 : id;
        record(layoutId, userId, now);
        return layoutId;
    }

    /** 한 revision 의 이름·항목(이력). 없으면 empty */
    public Optional<ObjectNode> findRevision(String siteId, long layoutId, int revision) {
        return jdbc.query("""
                SELECT r.layout_id, r.revision, r.name, r.items::text AS items, r.created_at
                FROM layout_plan_revision r JOIN layout_plan l ON l.layout_id = r.layout_id
                WHERE l.site_id = :site AND r.layout_id = :id AND r.revision = :revision
                """, new MapSqlParameterSource("site", siteId).addValue("id", layoutId).addValue("revision", revision),
                (rs, i) -> {
                    ObjectNode o = mapper.createObjectNode();
                    o.put("layout_id", rs.getLong("layout_id"));
                    o.put("revision", rs.getInt("revision"));
                    o.put("layout_version", "lv-" + rs.getInt("revision"));
                    o.put("name", rs.getString("name"));
                    o.set("items", mapper.readTree(rs.getString("items")));
                    o.put("created_at", rs.getTimestamp("created_at").getTime());
                    return o;
                }).stream().findFirst();
    }

    /** 지금 revision 을 이력에 남긴다(추가만). 같은 트랜잭션 안에서 부른다 */
    private void record(long layoutId, Long userId, Instant now) {
        jdbc.update("""
                INSERT INTO layout_plan_revision (layout_id, revision, name, items, created_by, created_at)
                SELECT layout_id, revision, name, items, :user, :now FROM layout_plan WHERE layout_id = :id
                """, new MapSqlParameterSource("id", layoutId).addValue("user", userId).addValue("now", utc(now)));
    }

    /**
     * 전체 바꾸기. revision 을 하나 올린다. 본문 revision 이 있으면 지금 값과 같을 때만 바꾼다(다른 사람이 먼저 고쳤으면 409).
     * 비교와 바꾸기를 한 문장으로 해 동시에 두 번 고쳐도 하나만 이긴다
     */
    public void replace(String siteId, long layoutId, LayoutRequest request, long userId, Instant now) {
        Checked c = check(request);
        int updated = jdbc.update("""
                UPDATE layout_plan SET name = :name, items = CAST(:items AS jsonb), revision = revision + 1, updated_at = :now
                WHERE site_id = :site AND layout_id = :id
                  AND (CAST(:expected AS integer) IS NULL OR revision = :expected)
                """, new MapSqlParameterSource().addValue("site", siteId).addValue("id", layoutId)
                .addValue("name", c.name()).addValue("items", c.items()).addValue("now", utc(now))
                .addValue("expected", request.revision()));
        if (updated == 0) {
            boolean exists = findOne(siteId, layoutId).isPresent();
            throw new BusinessException(exists ? ErrorCode.Layout.LAYOUT_REVISION_CONFLICT : ErrorCode.Layout.LAYOUT_NOT_FOUND);
        }
        record(layoutId, userId, now);
    }

    private record Checked(String name, String items) {
    }

    private Checked check(LayoutRequest r) {
        if (r == null || r.items() == null || r.items().size() > MAX_ITEMS) {
            throw invalid();
        }
        String name = text(r.name(), MAX_NAME);
        if (name == null) {
            throw invalid();
        }
        if (r.revision() != null && r.revision() < 1) {
            throw invalid();
        }
        ArrayNode items = mapper.createArrayNode();
        Set<String> ids = new HashSet<>();
        for (LayoutRequest.Item item : r.items()) {
            if (item == null) {
                throw invalid();
            }
            String id = text(item.itemId(), MAX_ITEM_ID);
            if (id == null || !ids.add(id) || item.kind() == null || !KINDS.contains(item.kind())) {
                throw invalid();
            }
            double[] center = finite(item.center(), false);
            double[] size = finite(item.size(), true);
            Double yaw = item.yawRad();
            if (yaw == null || !Double.isFinite(yaw) || Math.abs(yaw) > YAW_LIMIT_RAD) {
                throw invalid();
            }
            String label = item.label() == null || item.label().isBlank() ? null : text(item.label(), MAX_LABEL);
            ObjectNode o = items.addObject();
            o.put("item_id", id);
            o.put("kind", item.kind());
            o.putArray("center").add(center[0]).add(center[1]).add(center[2]);
            o.putArray("size").add(size[0]).add(size[1]).add(size[2]);
            o.put("yaw_rad", yaw);
            if (label == null) {
                o.putNull("label");
            } else {
                o.put("label", label);
            }
        }
        return new Checked(name, mapper.writeValueAsString(items));
    }

    /** 유한한 수 3개, |값| <= MAX_ABS_M. positive 면 모두 > 0 */
    private static double[] finite(List<Double> values, boolean positive) {
        if (values == null || values.size() != 3) {
            throw invalid();
        }
        double[] out = new double[3];
        for (int i = 0; i < 3; i++) {
            Double v = values.get(i);
            if (v == null || !Double.isFinite(v) || Math.abs(v) > MAX_ABS_M || (positive && !(v > 0))) {
                throw invalid();
            }
            out[i] = v;
        }
        return out;
    }

    /** 앞뒤 공백을 뺀다. 비면 null. 길이는 코드 포인트로 세고 PostgreSQL 이 받지 않는 NUL 은 거부한다 */
    private static String text(String value, int max) {
        String stripped = value == null ? "" : value.strip();
        if (stripped.isEmpty()) {
            return null;
        }
        if (stripped.indexOf(0) >= 0 || stripped.codePointCount(0, stripped.length()) > max) {
            throw invalid();
        }
        return stripped;
    }

    private static BusinessException invalid() {
        return new BusinessException(ErrorCode.Layout.INVALID_LAYOUT);
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }

    private ObjectNode summary(ResultSet rs) throws SQLException {
        ObjectNode l = mapper.createObjectNode();
        l.put("layout_id", rs.getLong("layout_id"));
        l.put("name", rs.getString("name"));
        int revision = rs.getInt("revision");
        l.put("revision", revision);
        l.put("layout_version", "lv-" + revision);
        l.put("source", rs.getString("source"));
        l.put("created_at", rs.getTimestamp("created_at").getTime());
        l.put("updated_at", rs.getTimestamp("updated_at").getTime());
        return l;
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        ObjectNode l = summary(rs);
        l.set("items", mapper.readTree(rs.getString("items")));
        return l;
    }
}
