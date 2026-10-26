package com.ssafy.simai.domain.zone;

import com.ssafy.simai.domain.config.Versioned;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.stream.Collectors;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 위험 구역(V1 zone, 명세 8.2·11.4). 수정은 지금 버전을 닫고(valid_to) 새 버전을 더한다. 지우지 않는다.
 * 다각형은 PostGIS(SRID 0, 로컬 미터)에 두고 저장·조회만 한다. 판정은 Tier 0 가 구성 묶음으로 받아서 한다.
 * 응답·구성 묶음의 polygon_xy 는 닫는 점을 뺀 꼭짓점 목록이다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ZoneRepository {

    static final Set<String> EVAL_TYPES = Set.of("GROUND_FOOTPRINT", "VOLUME_3D");
    static final Set<String> SEVERITIES = Set.of("CRITICAL", "HIGH", "CAUTION", "INFO");
    private static final int MAX_VERTICES = 256;

    private static final String COLUMNS = """
            zone_id, zone_version, site_id, name, zone_kind, eval_type, ST_AsGeoJSON(polygon) AS polygon, z_min_m, z_max_m,
            severity, requirements::text AS requirements, has_guardrail, valid_from, valid_to
            """;

    private static final String INSERT = """
            INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, z_min_m, z_max_m, severity,
                requirements, has_guardrail, valid_from, created_by)
            VALUES (:zoneId, :version, :siteId, :name, :zoneKind, :evalType, ST_GeomFromText(:wkt, 0), :zMin, :zMax, :severity,
                CAST(:requirements AS jsonb), :hasGuardrail, :validFrom, :createdBy)
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public ZoneRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 지금 유효한 버전(valid_to 없음)만, zone_id 순 */
    public List<ObjectNode> findCurrent(String siteId) {
        return jdbc.query("SELECT " + COLUMNS + " FROM zone WHERE site_id = :siteId AND valid_to IS NULL ORDER BY zone_id",
                new MapSqlParameterSource("siteId", siteId), (rs, i) -> view(rs));
    }

    public List<ObjectNode> findVersions(String zoneId) {
        List<ObjectNode> versions = jdbc.query("SELECT " + COLUMNS + " FROM zone WHERE zone_id = :zoneId ORDER BY zone_version",
                new MapSqlParameterSource("zoneId", zoneId), (rs, i) -> view(rs));
        if (versions.isEmpty()) {
            throw new BusinessException(ErrorCode.Config.ZONE_NOT_FOUND);
        }
        return versions;
    }

    /** 한 트랜잭션 안에서 부른다. 다각형이 꼬였으면(자기 교차·넓이 0) 실패한다 */
    public ObjectNode create(String siteId, ZoneRequest request, long userId, OffsetDateTime now) {
        Checked checked = check(request);
        String zoneId = Versioned.unusedId("z", this::exists);
        insert(zoneId, 1, siteId, checked, userId, now);
        return findVersion(zoneId, 1);
    }

    /** 지금 버전을 잠그고 닫은 뒤 다음 버전을 더한다. 한 트랜잭션 안에서 부른다 */
    public ObjectNode newVersion(String zoneId, ZoneRequest request, long userId, OffsetDateTime now) {
        Checked checked = check(request);
        MapSqlParameterSource params = new MapSqlParameterSource("zoneId", zoneId).addValue("now", now);
        List<Integer> current = jdbc.queryForList("""
                SELECT zone_version FROM zone WHERE zone_id = :zoneId AND valid_to IS NULL FOR UPDATE
                """, params, Integer.class);
        if (current.isEmpty()) {
            // 같이 고치던 다른 요청이 먼저 새 버전을 만들었으면 지금 버전이 바뀌었다(잠금이 풀린 뒤 다시 읽음)
            throw new BusinessException(exists(zoneId) ? ErrorCode.Config.CONCURRENT_EDIT : ErrorCode.Config.ZONE_NOT_FOUND);
        }
        String siteId = jdbc.queryForObject("SELECT site_id FROM zone WHERE zone_id = :zoneId LIMIT 1", params, String.class);
        jdbc.update("UPDATE zone SET valid_to = :now WHERE zone_id = :zoneId AND valid_to IS NULL", params);
        int version = current.getFirst() + 1;
        insert(zoneId, version, siteId, checked, userId, now);
        return findVersion(zoneId, version);
    }

    private boolean exists(String zoneId) {
        return !jdbc.queryForList("SELECT 1 FROM zone WHERE zone_id = :zoneId LIMIT 1",
                new MapSqlParameterSource("zoneId", zoneId), Integer.class).isEmpty();
    }

    private ObjectNode findVersion(String zoneId, int version) {
        return jdbc.queryForObject("SELECT " + COLUMNS + " FROM zone WHERE zone_id = :zoneId AND zone_version = :v",
                new MapSqlParameterSource("zoneId", zoneId).addValue("v", version), (rs, i) -> view(rs));
    }

    private void insert(String zoneId, int version, String siteId, Checked c, long userId, OffsetDateTime now) {
        jdbc.update(INSERT, new MapSqlParameterSource()
                .addValue("zoneId", zoneId).addValue("version", version).addValue("siteId", siteId)
                .addValue("name", c.name()).addValue("zoneKind", c.zoneKind()).addValue("evalType", c.evalType())
                .addValue("wkt", c.wkt()).addValue("zMin", c.zMin()).addValue("zMax", c.zMax())
                .addValue("severity", c.severity()).addValue("requirements", c.requirements())
                .addValue("hasGuardrail", c.hasGuardrail()).addValue("validFrom", now).addValue("createdBy", userId));
    }

    private record Checked(String name, String zoneKind, String evalType, String wkt, Double zMin, Double zMax, String severity,
            String requirements, Boolean hasGuardrail) {
    }

    private Checked check(ZoneRequest r) {
        if (r == null || Versioned.blank(r.name()) || Versioned.blank(r.zoneKind())
                || r.evalType() == null || !EVAL_TYPES.contains(r.evalType())
                || r.severity() == null || !SEVERITIES.contains(r.severity())
                || (r.requirements() != null && !r.requirements().isNull() && !r.requirements().isObject())) {
            throw new BusinessException(ErrorCode.Config.INVALID_ZONE);
        }
        // real 열에 들어간 값으로 검사한다(최저 < 최고가 저장 뒤에도 성립해야 Tier 0 가 받는다)
        Double zMin = r.zMinM() == null ? null : Versioned.real(r.zMinM(), ErrorCode.Config.INVALID_Z_RANGE);
        Double zMax = r.zMaxM() == null ? null : Versioned.real(r.zMaxM(), ErrorCode.Config.INVALID_Z_RANGE);
        if (zMin != null && zMax != null && !(zMin < zMax)) {
            throw new BusinessException(ErrorCode.Config.INVALID_Z_RANGE);
        }
        if ("VOLUME_3D".equals(r.evalType()) && (zMin == null || zMax == null)) {
            throw new BusinessException(ErrorCode.Config.INVALID_Z_RANGE);
        }
        String wkt = wkt(r.polygonXy());
        Boolean valid = jdbc.queryForObject("""
                SELECT ST_IsValid(g) AND ST_Area(g) > 0 FROM (SELECT ST_GeomFromText(:wkt, 0) AS g) s
                """, new MapSqlParameterSource("wkt", wkt), Boolean.class);
        if (!Boolean.TRUE.equals(valid)) {
            throw new BusinessException(ErrorCode.Config.INVALID_POLYGON);
        }
        JsonNode requirements = r.requirements();
        return new Checked(r.name().strip(), r.zoneKind().strip(), r.evalType(), wkt, zMin, zMax, r.severity(),
                requirements == null || requirements.isNull() ? null : mapper.writeValueAsString(requirements),
                r.hasGuardrail());
    }

    /** 꼭짓점 3개 이상(닫는 점 생략 가능), 유한한 수. 닫힌 WKT 로 만든다 */
    private static String wkt(List<List<Double>> points) {
        if (points == null || points.size() < 3 || points.size() > MAX_VERTICES + 1) {
            throw new BusinessException(ErrorCode.Config.INVALID_POLYGON);
        }
        List<double[]> ring = new ArrayList<>();
        for (List<Double> point : points) {
            ring.add(Versioned.finite(point, 2, ErrorCode.Config.INVALID_POLYGON));
        }
        if (Arrays.equals(ring.getFirst(), ring.getLast())) {
            ring.removeLast();
        }
        if (ring.size() < 3 || ring.size() > MAX_VERTICES) {
            throw new BusinessException(ErrorCode.Config.INVALID_POLYGON);
        }
        ring.add(ring.getFirst());
        return ring.stream().map(p -> String.format(Locale.ROOT, "%s %s", p[0], p[1]))
                .collect(Collectors.joining(", ", "POLYGON((", "))"));
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        ObjectNode z = mapper.createObjectNode();
        z.put("zone_id", rs.getString("zone_id"));
        z.put("zone_version", rs.getInt("zone_version"));
        z.put("name", rs.getString("name"));
        z.put("zone_kind", rs.getString("zone_kind"));
        z.put("eval_type", rs.getString("eval_type"));
        Versioned.polygonXy(mapper.readTree(rs.getString("polygon")), z.putArray("polygon_xy"));
        z.put("z_min_m", Versioned.real(rs, "z_min_m"));
        z.put("z_max_m", Versioned.real(rs, "z_max_m"));
        z.put("severity", rs.getString("severity"));
        String requirements = rs.getString("requirements");
        z.set("requirements", requirements == null ? null : mapper.readTree(requirements));
        boolean guardrail = rs.getBoolean("has_guardrail");
        if (rs.wasNull()) {
            z.putNull("has_guardrail");
        } else {
            z.put("has_guardrail", guardrail);
        }
        z.put("valid_from", Versioned.millis(rs.getObject("valid_from", OffsetDateTime.class)));
        z.put("valid_to", Versioned.millis(rs.getObject("valid_to", OffsetDateTime.class)));
        return z;
    }
}
