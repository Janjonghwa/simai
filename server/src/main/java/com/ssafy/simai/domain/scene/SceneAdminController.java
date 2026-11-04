package com.ssafy.simai.domain.scene;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Set;
import java.util.regex.Pattern;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 관리자가 이미 만든 glb 를 Scene 으로 등록하고 확정한다(S15P31A101-322, 운영 시드 4단계 docs/design/ec2-deploy.md, 명세 3.7·4.5·12.6).
 * 워커가 복원·검증해 커밋하는 흐름(POST /api/scenes/rebuild, S15P31A101-137)과 같은 scene_version 상태(CANDIDATE → COMMITTED·REJECTED)를 쓴다.
 * <pre>
 * POST /api/scenes                       {scene_version, calibration_version, gltf_key, quality?} → 201 CANDIDATE
 * POST /api/scenes/{sceneVersion}/commit                                        → COMMITTED(이전 현재 Scene 은 valid_to 로 닫음)
 * POST /api/scenes/{sceneVersion}/reject {reason}                               → REJECTED
 * GET  /api/scenes/{sceneVersion}                                               → 상태
 * </pre>
 * glb 는 관리자가 POST /api/uploads/sign(purpose SCENE_GLTF)으로 받은 주소에 올린 객체여야 한다. 키를 아무거나 받지 않는다(남의 객체·없는 객체).
 * 현재 Scene(GET /api/scenes/current)은 COMMITTED 이고 끝나지 않은 가장 최근 것이라, 확정하면 바로 그 Scene 이 나온다.
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SceneAdminController {

    /** 계약 version_id 는 비어 있지만 않으면 된다. 경로에 쓰이므로 좁게 받고, current 는 GET /api/scenes/current 와 겹쳐 받지 않는다 */
    private static final Pattern VERSION = Pattern.compile("^[a-z0-9][a-z0-9._-]{0,62}$");
    /** 관리자가 확정·거절하는 상태. VALIDATING 은 워커 검증(-137)이 쥔 상태라 관리자 경로에서 건드리지 않는다 */
    private static final Set<String> OPEN_STATES = Set.of("CANDIDATE");
    private static final int MAX_REASON = 500;

    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final ObjectMapper mapper;
    private final SiteLookup sites;
    private final Clock clock;
    private final SceneActivationService activations;

    SceneAdminController(NamedParameterJdbcTemplate jdbc, TransactionTemplate transaction, ObjectMapper mapper, SiteLookup sites,
            Clock clock, SceneActivationService activations) {
        this.jdbc = jdbc;
        this.transaction = transaction;
        this.mapper = mapper;
        this.sites = sites;
        this.clock = clock;
        this.activations = activations;
    }

    @PostMapping("/api/scenes")
    @PreAuthorize("hasRole('ADMIN')")
    @ResponseStatus(HttpStatus.CREATED)
    ObjectNode register(@RequestBody(required = false) JsonNode body, @AuthenticationPrincipal Jwt jwt) {
        String version = version(text(body, "scene_version"));
        String calibration = text(body, "calibration_version");
        String key = text(body, "gltf_key");
        JsonNode quality = body.get("quality");
        if (calibration == null || key == null || (quality != null && !quality.isNull() && !quality.isObject())) {
            throw new BusinessException(ErrorCode.Scene.INVALID_SCENE);
        }
        String siteId = sites.siteId();
        long userId = Long.parseLong(jwt.getSubject());
        return transaction.execute(status -> {
            MapSqlParameterSource params = new MapSqlParameterSource().addValue("v", version).addValue("cal", calibration)
                    .addValue("key", key).addValue("site", siteId).addValue("user", userId)
                    .addValue("quality", quality == null || quality.isNull() ? null : mapper.writeValueAsString(quality))
                    .addValue("now", Timestamp.from(now()));
            if (!jdbc.queryForList("SELECT 1 FROM scene_version WHERE scene_version = :v", params).isEmpty()) {
                throw new BusinessException(ErrorCode.Scene.SCENE_EXISTS);
            }
            List<String> calibrationState = jdbc.queryForList(
                    "SELECT state FROM calibration_version WHERE calibration_version = :cal AND site_id = :site", params, String.class);
            if (calibrationState.isEmpty() || !SceneActivationService.CALIBRATION_STATES.contains(calibrationState.getFirst())) {
                throw new BusinessException(ErrorCode.Scene.SCENE_CALIBRATION_INVALID);
            }
            // 발급을 쓰인 것으로 표시한다(행 잠금). 같은 glb 로 두 Scene 을 만들지 않는다
            if (jdbc.update("""
                    UPDATE upload_grant SET used_at = :now
                    WHERE object_key = :key AND purpose = 'SCENE_GLTF' AND used_at IS NULL
                    """, params) == 0) {
                throw new BusinessException(ErrorCode.Scene.SCENE_GLTF_NOT_UPLOADED);
            }
            try {
                jdbc.update("""
                        INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, quality,
                            is_upper_bound, requested_by)
                        VALUES (:v, :site, :cal, 'CANDIDATE', :key, CAST(:quality AS jsonb), false, :user)
                        """, params);
            } catch (DuplicateKeyException e) {
                // 같은 버전을 다른 glb 로 동시에 등록했다(앞의 존재 확인 뒤에 끼어든 쪽)
                throw new BusinessException(ErrorCode.Scene.SCENE_EXISTS);
            }
            return view(version);
        });
    }

    @PostMapping("/api/scenes/{sceneVersion}/commit")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode commit(@PathVariable String sceneVersion, @AuthenticationPrincipal Jwt jwt) {
        return transaction.execute(status -> {
            activations.lock();
            MapSqlParameterSource params = new MapSqlParameterSource("v", sceneVersion);
            requireOpen(params);
            String site = jdbc.queryForObject("SELECT site_id FROM scene_version WHERE scene_version = :v", params, String.class);
            activations.activate(site, sceneVersion, "user:" + jwt.getSubject(), "관리자 Scene 확정 API");
            return view(sceneVersion);
        });
    }

    @PostMapping("/api/scenes/{sceneVersion}/reject")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode reject(@PathVariable String sceneVersion, @RequestBody(required = false) JsonNode body) {
        String reason = text(body, "reason");
        if (reason == null || reason.length() > MAX_REASON) {
            throw new BusinessException(ErrorCode.Scene.INVALID_SCENE);
        }
        return transaction.execute(status -> {
            MapSqlParameterSource params = new MapSqlParameterSource("v", sceneVersion).addValue("reason", reason);
            requireOpen(params);
            jdbc.update("UPDATE scene_version SET state = 'REJECTED', rejected_reason = :reason WHERE scene_version = :v", params);
            return view(sceneVersion);
        });
    }

    @GetMapping("/api/scenes/{sceneVersion}")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode get(@PathVariable String sceneVersion) {
        return view(sceneVersion);
    }

    /** 행을 잠그고 아직 확정·거절 전인지 본다 */
    private void requireOpen(MapSqlParameterSource params) {
        List<String> state = jdbc.queryForList("SELECT state FROM scene_version WHERE scene_version = :v FOR UPDATE", params,
                String.class);
        if (state.isEmpty()) {
            throw new BusinessException(ErrorCode.Scene.SCENE_VERSION_NOT_FOUND);
        }
        if (!OPEN_STATES.contains(state.getFirst())) {
            throw new BusinessException(ErrorCode.Scene.SCENE_STATE_CONFLICT);
        }
    }

    /** 응답에는 객체 키를 싣지 않는다(재생은 GET /api/scenes/current 의 서명 주소로) */
    private ObjectNode view(String sceneVersion) {
        List<ObjectNode> found = jdbc.query("""
                SELECT scene_version, site_id, calibration_version, state, quality::text AS quality, committed_at, rejected_reason,
                    valid_from, valid_to, requested_by
                FROM scene_version WHERE scene_version = :v
                """, new MapSqlParameterSource("v", sceneVersion), (rs, i) -> row(rs));
        if (found.isEmpty()) {
            throw new BusinessException(ErrorCode.Scene.SCENE_VERSION_NOT_FOUND);
        }
        return found.getFirst();
    }

    private ObjectNode row(ResultSet rs) throws SQLException {
        ObjectNode n = mapper.createObjectNode();
        n.put("scene_version", rs.getString("scene_version"));
        n.put("site_id", rs.getString("site_id"));
        n.put("calibration_version", rs.getString("calibration_version"));
        n.put("state", rs.getString("state"));
        String quality = rs.getString("quality");
        n.set("quality", quality == null ? mapper.nullNode() : mapper.readTree(quality));
        millis(n, "committed_at_ms", rs.getTimestamp("committed_at"));
        n.put("rejected_reason", rs.getString("rejected_reason"));
        millis(n, "valid_from_ms", rs.getTimestamp("valid_from"));
        millis(n, "valid_to_ms", rs.getTimestamp("valid_to"));
        long by = rs.getLong("requested_by");
        if (rs.wasNull()) {
            n.putNull("requested_by");
        } else {
            n.put("requested_by", by);
        }
        return n;
    }

    private static void millis(ObjectNode node, String field, Timestamp time) {
        if (time == null) {
            node.putNull(field);
        } else {
            node.put(field, time.getTime());
        }
    }

    private static String version(String value) {
        if (value == null || !VERSION.matcher(value).matches() || value.equals("current") || value.equals("regenerate")) {
            throw new BusinessException(ErrorCode.Scene.INVALID_SCENE);
        }
        return value;
    }

    /** 문자열 칸. 없으면 null. 본문이 객체가 아니거나 칸이 문자열이 아니면 INVALID_SCENE */
    private static String text(JsonNode body, String field) {
        if (body == null || !body.isObject()) {
            throw new BusinessException(ErrorCode.Scene.INVALID_SCENE);
        }
        JsonNode node = body.get(field);
        if (node == null || node.isNull()) {
            return null;
        }
        if (!node.isString() || node.stringValue().isBlank()) {
            throw new BusinessException(ErrorCode.Scene.INVALID_SCENE);
        }
        return node.stringValue().strip();
    }

    private Instant now() {
        return clock.instant().truncatedTo(ChronoUnit.MILLIS);
    }
}
