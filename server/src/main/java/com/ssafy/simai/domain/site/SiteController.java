package com.ssafy.simai.domain.site;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.Timestamp;
import java.time.Clock;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현장(site) 등록·수정(S15P31A101-322, 운영 시드 2단계 docs/design/ec2-deploy.md, 명세 11.1·12.6). MVP 는 현장 하나다(16장, SiteLookup).
 * <pre>
 * GET  /api/sites/current   → {site_id, name, world_origin, established_calibration_version, created_at_ms}  (로그인)
 * POST /api/sites           {site_id, name, world_origin?} → 201  (관리자, 현장이 없을 때만)
 * PUT  /api/sites/current   {site_id?, name?, world_origin?}       (관리자)
 * </pre>
 * Flyway V2 가 개발 현장(classroom-1)과 카메라를 늘 시드하므로, 운영 현장 ID 로 바꾸는 일은 PUT 의 site_id 로 한다. ID 는 보정 문서의
 * coordinate_frame.site_id 와 같아야 하고 구역·보정·Scene·구성 묶음이 모두 이 ID 에 붙으므로, 그런 기록이 하나라도 붙은 뒤에는 바꾸지 않는다
 * (카메라는 함께 옮긴다). 첫 운영 배포 초기화 직후 보정을 올리기 전에 한 번 바꾸는 용도다.
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SiteController {

    /** 소문자·숫자·- 로 1~63자(V2 의 classroom-1 과 같은 꼴). 객체 키·구성 묶음 경로에도 들어가므로 좁게 받는다 */
    private static final Pattern SITE_ID = Pattern.compile("^[a-z0-9][a-z0-9-]{0,62}$");
    private static final int MAX_NAME = 100;
    /** site 를 참조하는 기록(카메라 제외). 하나라도 있으면 site_id 를 바꾸지 않는다 */
    private static final List<String> DEPENDENTS = List.of("zone", "static_object", "config_bundle", "calibration_version",
            "calibration_run", "scale_reference", "floor_plan", "scene_version", "bev_frame_set", "work_unit", "layout_plan", "setup_session");

    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final ObjectMapper mapper;
    private final Clock clock;

    SiteController(NamedParameterJdbcTemplate jdbc, TransactionTemplate transaction, ObjectMapper mapper, Clock clock) {
        this.jdbc = jdbc;
        this.transaction = transaction;
        this.mapper = mapper;
        this.clock = clock;
    }

    @GetMapping("/api/sites/current")
    ObjectNode current() {
        return view(single());
    }

    @PostMapping("/api/sites")
    @PreAuthorize("hasRole('ADMIN')")
    @ResponseStatus(HttpStatus.CREATED)
    ObjectNode register(@RequestBody(required = false) JsonNode body) {
        String siteId = siteId(body);
        String name = name(body);
        String origin = worldOrigin(body);
        return transaction.execute(status -> {
            // 현장 하나(MVP). 동시에 둘이 등록하지 못하게 표를 잠근다
            jdbc.getJdbcTemplate().execute("LOCK TABLE site IN SHARE ROW EXCLUSIVE MODE");
            if (!jdbc.queryForList("SELECT 1 FROM site LIMIT 1", Map.of()).isEmpty()) {
                throw new BusinessException(ErrorCode.Site.SITE_EXISTS);
            }
            jdbc.update("INSERT INTO site (site_id, name, world_origin, created_at) VALUES (:id, :name, CAST(:origin AS jsonb), :now)",
                    new MapSqlParameterSource().addValue("id", siteId).addValue("name", name).addValue("origin", origin)
                            .addValue("now", Timestamp.from(clock.instant())));
            return view(siteId);
        });
    }

    /** 바꿀 칸만 보낸다. world_origin 을 null 로 보내면 비운다 */
    @PutMapping("/api/sites/current")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode update(@RequestBody(required = false) JsonNode body) {
        if (body == null || !body.isObject() || body.isEmpty()) {
            throw new BusinessException(ErrorCode.Site.INVALID_SITE);
        }
        if (!body.has("site_id") && !body.has("name") && !body.has("world_origin")) {
            // 아는 칸이 하나도 없으면 아무것도 바꾸지 않은 채 200 을 주지 않는다(오타 등)
            throw new BusinessException(ErrorCode.Site.INVALID_SITE);
        }
        String newId = body.has("site_id") ? siteId(body) : null;
        String name = body.has("name") ? name(body) : null;
        boolean setOrigin = body.has("world_origin");
        String origin = setOrigin ? worldOrigin(body) : null;
        return transaction.execute(status -> {
            jdbc.getJdbcTemplate().execute("LOCK TABLE site IN SHARE ROW EXCLUSIVE MODE");
            String current = single();
            MapSqlParameterSource params = new MapSqlParameterSource("id", current).addValue("name", name)
                    .addValue("origin", origin).addValue("setOrigin", setOrigin);
            jdbc.update("""
                    UPDATE site SET name = COALESCE(:name, name),
                        world_origin = CASE WHEN :setOrigin THEN CAST(:origin AS jsonb) ELSE world_origin END
                    WHERE site_id = :id
                    """, params);
            if (newId == null || newId.equals(current)) {
                return view(current);
            }
            for (String table : DEPENDENTS) {
                if (!jdbc.queryForList("SELECT 1 FROM " + table + " WHERE site_id = :id LIMIT 1", params).isEmpty()) {
                    throw new BusinessException(ErrorCode.Site.SITE_IN_USE);
                }
            }
            // 키를 바꾸면 카메라 FK 가 걸린다. 새 행을 만들고 카메라를 옮긴 뒤 옛 행을 지운다
            jdbc.update("""
                    INSERT INTO site (site_id, name, world_origin, established_calibration_version, current_floor_plan_id,
                        default_camera_heights_m, migrated_from, created_at)
                    SELECT :newId, name, world_origin, established_calibration_version, current_floor_plan_id, default_camera_heights_m, migrated_from, created_at

                    FROM site WHERE site_id = :id
                    """, params.addValue("newId", newId));
            jdbc.update("UPDATE camera SET site_id = :newId WHERE site_id = :id", params);
            jdbc.update("DELETE FROM site WHERE site_id = :id", params);
            return view(newId);
        });
    }

    /** 현장 하나의 ID. 없거나 둘 이상이면 SITE_NOT_SINGLE(SiteLookup 과 같은 규칙) */
    private String single() {
        List<String> sites = jdbc.queryForList("SELECT site_id FROM site ORDER BY site_id LIMIT 2", Map.of(), String.class);
        if (sites.size() != 1) {
            throw new BusinessException(ErrorCode.Config.SITE_NOT_SINGLE);
        }
        return sites.getFirst();
    }

    private ObjectNode view(String siteId) {
        return jdbc.queryForObject("""
                SELECT site_id, name, world_origin::text AS world_origin, established_calibration_version, created_at FROM site
                WHERE site_id = :id
                """, new MapSqlParameterSource("id", siteId), (rs, i) -> {
            ObjectNode n = mapper.createObjectNode();
            n.put("site_id", rs.getString("site_id"));
            n.put("name", rs.getString("name"));
            String origin = rs.getString("world_origin");
            n.set("world_origin", origin == null ? mapper.nullNode() : mapper.readTree(origin));
            n.put("established_calibration_version", rs.getString("established_calibration_version"));
            n.put("created_at_ms", rs.getTimestamp("created_at").getTime());
            return n;
        });
    }

    private static String siteId(JsonNode body) {
        JsonNode node = body == null ? null : body.get("site_id");
        if (node == null || !node.isString() || !SITE_ID.matcher(node.stringValue()).matches()) {
            throw new BusinessException(ErrorCode.Site.INVALID_SITE);
        }
        return node.stringValue();
    }

    private static String name(JsonNode body) {
        JsonNode node = body == null ? null : body.get("name");
        if (node == null || !node.isString() || node.stringValue().isBlank() || node.stringValue().strip().length() > MAX_NAME) {
            throw new BusinessException(ErrorCode.Site.INVALID_SITE);
        }
        return node.stringValue().strip();
    }

    /** 객체 또는 null(없음). 원점·축 선택 근거(4.1)는 보정이 정하므로 모양은 검사하지 않고 객체인지만 본다 */
    private String worldOrigin(JsonNode body) {
        JsonNode node = body == null ? null : body.get("world_origin");
        if (node == null || node.isNull()) {
            return null;
        }
        if (!node.isObject()) {
            throw new BusinessException(ErrorCode.Site.INVALID_SITE);
        }
        return mapper.writeValueAsString(node);
    }
}
