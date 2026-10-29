package com.ssafy.simai.domain.geometry;

import com.ssafy.simai.domain.config.Versioned;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 등록 작업면·정적 객체(V1 static_object, 명세 7.5). 수정은 지금 버전을 닫고 새 버전을 더한다. 보관은 지금 버전만 닫는다.
 * obb 열은 {"center":[x,y,z],"size":[sx,sy,sz],"yaw_rad":r}, 작업면 상판 높이는 top_height_m 열, 높이 표준편차는 obb 안
 * top_height_sigma_m(선택)에 둔다(V1 에 열이 없다).
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class StaticObjectRepository {

    static final String WORK_SURFACE = "WORK_SURFACE";
    static final Set<String> KINDS = Set.of(WORK_SURFACE, "OBSTACLE");

    private static final String COLUMNS = """
            object_id, object_version, kind, name, obb::text AS obb, top_height_m, valid_from, valid_to
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public StaticObjectRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    public List<ObjectNode> findCurrent(String siteId) {
        return jdbc.query("SELECT " + COLUMNS + " FROM static_object WHERE site_id = :siteId AND valid_to IS NULL ORDER BY object_id",
                new MapSqlParameterSource("siteId", siteId), (rs, i) -> view(rs));
    }

    public List<ObjectNode> findVersions(String objectId) {
        List<ObjectNode> versions = jdbc.query("SELECT " + COLUMNS + " FROM static_object WHERE object_id = :id ORDER BY object_version",
                new MapSqlParameterSource("id", objectId), (rs, i) -> view(rs));
        if (versions.isEmpty()) {
            throw new BusinessException(ErrorCode.Config.STATIC_OBJECT_NOT_FOUND);
        }
        return versions;
    }

    public ObjectNode create(String siteId, StaticObjectRequest request, long userId, OffsetDateTime now) {
        Checked checked = check(request);
        String objectId = Versioned.unusedId(WORK_SURFACE.equals(checked.kind()) ? "ws" : "so", this::exists);
        insert(objectId, 1, siteId, checked, userId, now);
        return findVersion(objectId, 1);
    }

    public ObjectNode newVersion(String objectId, StaticObjectRequest request, long userId, OffsetDateTime now) {
        Checked checked = check(request);
        MapSqlParameterSource params = new MapSqlParameterSource("id", objectId).addValue("now", now);
        List<Integer> current = jdbc.queryForList(
                "SELECT object_version FROM static_object WHERE object_id = :id AND valid_to IS NULL FOR UPDATE", params, Integer.class);
        if (current.isEmpty()) {
            throw notCurrent(objectId);
        }
        String siteId = jdbc.queryForObject("SELECT site_id FROM static_object WHERE object_id = :id LIMIT 1", params, String.class);
        jdbc.update("UPDATE static_object SET valid_to = :now WHERE object_id = :id AND valid_to IS NULL", params);
        int version = current.getFirst() + 1;
        insert(objectId, version, siteId, checked, userId, now);
        return findVersion(objectId, version);
    }

    /**
     * 지금 버전을 닫는다. 다음 구성 묶음부터 빠진다. 한 트랜잭션 안에서 부른다.
     * 이미 보관한 항목을 다시 보관하면 그대로 성공이다(두 번 누름·재시도). 없는 ID 만 404 다
     */
    public void archive(String objectId, OffsetDateTime now) {
        int closed = jdbc.update("UPDATE static_object SET valid_to = :now WHERE object_id = :id AND valid_to IS NULL",
                new MapSqlParameterSource("id", objectId).addValue("now", now));
        boolean alreadyArchived = closed == 0 && exists(objectId) && !hasCurrent(objectId);
        if (closed == 0 && !alreadyArchived) {
            throw notCurrent(objectId);
        }
    }

    /** 다른 요청이 먼저 새 버전을 만들었으면 충돌, 지금 버전이 없으면(없는 ID·보관한 객체) 없는 것 */
    private BusinessException notCurrent(String objectId) {
        boolean current = hasCurrent(objectId);
        return new BusinessException(current ? ErrorCode.Config.CONCURRENT_EDIT : ErrorCode.Config.STATIC_OBJECT_NOT_FOUND);
    }

    private boolean hasCurrent(String objectId) {
        return !jdbc.queryForList("SELECT 1 FROM static_object WHERE object_id = :id AND valid_to IS NULL LIMIT 1",
                new MapSqlParameterSource("id", objectId), Integer.class).isEmpty();
    }

    private boolean exists(String objectId) {
        return !jdbc.queryForList("SELECT 1 FROM static_object WHERE object_id = :id LIMIT 1",
                new MapSqlParameterSource("id", objectId), Integer.class).isEmpty();
    }

    private ObjectNode findVersion(String objectId, int version) {
        return jdbc.queryForObject("SELECT " + COLUMNS + " FROM static_object WHERE object_id = :id AND object_version = :v",
                new MapSqlParameterSource("id", objectId).addValue("v", version), (rs, i) -> view(rs));
    }

    private void insert(String objectId, int version, String siteId, Checked c, long userId, OffsetDateTime now) {
        jdbc.update("""
                INSERT INTO static_object (object_id, object_version, site_id, kind, obb, top_height_m, name, valid_from, created_by)
                VALUES (:id, :version, :siteId, :kind, CAST(:obb AS jsonb), :top, :name, :now, :createdBy)
                """, new MapSqlParameterSource()
                .addValue("id", objectId).addValue("version", version).addValue("siteId", siteId)
                .addValue("kind", c.kind()).addValue("obb", c.obb()).addValue("top", c.topHeight())
                .addValue("name", c.name()).addValue("now", now).addValue("createdBy", userId));
    }

    private record Checked(String kind, String name, String obb, Double topHeight) {
    }

    private Checked check(StaticObjectRequest r) {
        ErrorCode invalid = ErrorCode.Config.INVALID_STATIC_OBJECT;
        if (r == null || r.kind() == null || !KINDS.contains(r.kind()) || r.obb() == null) {
            throw new BusinessException(invalid);
        }
        double[] center = Versioned.finite(r.obb().center(), 3, invalid);
        double[] size = Versioned.finite(r.obb().size(), 3, invalid);
        double yaw = r.obb().yawRad() == null ? 0.0 : r.obb().yawRad();
        if (!Double.isFinite(yaw) || size[0] <= 0 || size[1] <= 0 || size[2] <= 0) {
            throw new BusinessException(invalid);
        }
        // 상판 높이는 real 열에 들어간 값으로 검사한다(아주 작은 값이 0 이 되면 Tier 0 가 그 작업면을 뺀다)
        Double top = r.topHeightM() == null ? null : Versioned.real(r.topHeightM(), invalid);
        // 작업면은 바닥(Z=0)보다 높은 상판이 있어야 R2·R3·ContactEstimator 가 쓴다(7.5)
        if (WORK_SURFACE.equals(r.kind()) && (top == null || !(top > 0))) {
            throw new BusinessException(invalid);
        }
        Double sigma = r.topHeightSigmaM();
        if (sigma != null && (!Double.isFinite(sigma) || sigma < 0)) {
            throw new BusinessException(invalid);
        }
        ObjectNode obb = mapper.createObjectNode();
        obb.putArray("center").add(center[0]).add(center[1]).add(center[2]);
        obb.putArray("size").add(size[0]).add(size[1]).add(size[2]);
        obb.put("yaw_rad", yaw);
        if (sigma != null) {
            obb.put("top_height_sigma_m", sigma);
        }
        String name = r.name() == null || r.name().isBlank() ? null : r.name().strip();
        return new Checked(r.kind(), name, mapper.writeValueAsString(obb), top);
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        ObjectNode o = mapper.createObjectNode();
        o.put("object_id", rs.getString("object_id"));
        o.put("object_version", rs.getInt("object_version"));
        o.put("kind", rs.getString("kind"));
        o.put("name", rs.getString("name"));
        ObjectNode obb = (ObjectNode) mapper.readTree(rs.getString("obb"));
        JsonNode sigma = obb.remove("top_height_sigma_m");
        o.set("obb", obb);
        o.put("top_height_m", Versioned.real(rs, "top_height_m"));
        if (sigma != null) {
            o.set("top_height_sigma_m", sigma);
        }
        o.put("valid_from", Versioned.millis(rs.getObject("valid_from", OffsetDateTime.class)));
        o.put("valid_to", Versioned.millis(rs.getObject("valid_to", OffsetDateTime.class)));
        return o;
    }
}
