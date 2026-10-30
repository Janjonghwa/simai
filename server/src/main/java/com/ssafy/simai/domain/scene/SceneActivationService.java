package com.ssafy.simai.domain.scene;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.sql.Timestamp;
import java.time.Clock;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** Scene 내용은 유지하고 활성화할 때마다 이력을 추가한다. CLI 와 관리자 확정 API 가 공유한다. */
@Service
public class SceneActivationService {

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectStorage storage;
    private final ObjectMapper mapper;
    private final Clock clock;

    public SceneActivationService(NamedParameterJdbcTemplate jdbc, ObjectStorage storage, ObjectMapper mapper, Clock clock) {
        this.jdbc = jdbc;
        this.storage = storage;
        this.mapper = mapper;
        this.clock = clock;
    }

    @Transactional
    public void activate(String siteId, String version, String actor, String reason) {
        if (actor == null || actor.isBlank() || reason == null || reason.isBlank() || reason.length() > 500) {
            throw new BusinessException(ErrorCode.Scene.INVALID_SCENE);
        }
        lock();
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("site", siteId).addValue("v", version)
                .addValue("actor", actor).addValue("reason", reason).addValue("now", Timestamp.from(clock.instant()));
        List<Map<String, Object>> rows = jdbc.queryForList("""
                SELECT s.*, s.scene_output::text AS output, c.state AS calibration_state FROM scene_version s
                JOIN calibration_version c ON c.calibration_version = s.calibration_version AND c.site_id = s.site_id
                WHERE s.scene_version = :v AND s.site_id = :site FOR UPDATE OF s
                """, params);
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Scene.SCENE_VERSION_NOT_FOUND);
        }
        Map<String, Object> row = rows.getFirst();
        if (!Set.of("CANDIDATE", "COMMITTED").contains(row.get("state")) || Boolean.TRUE.equals(row.get("is_upper_bound"))) {
            throw new BusinessException(ErrorCode.Scene.SCENE_STATE_CONFLICT);
        }
        // 워커 생성은 CALIBRATED 뒤에만 활성화(4.5). 직접 등록의 PROVISIONAL 예외는 유지한다(12.6).
        boolean worker = row.get("job_id") != null;
        if (!(worker ? Set.of("CALIBRATED") : Set.of("PROVISIONAL", "CALIBRATED")).contains(row.get("calibration_state"))) {
            throw new BusinessException(ErrorCode.Scene.SCENE_CALIBRATION_INVALID);
        }
        requireAssets(row, params, worker);
        List<String> current = jdbc.queryForList("SELECT scene_version FROM (" + SceneService.CURRENT_COMMITTED
                + ") s WHERE site_id = :site", params, String.class);
        String previous = current.isEmpty() ? null : current.getFirst();
        if (version.equals(previous)) {
            return;
        }
        params.addValue("previous", previous);
        // 이전 확정 시각은 보존한다. 재활성화 기간의 정본은 scene_activation 이다.
        jdbc.update("""
                UPDATE scene_version SET valid_to = :now
                WHERE site_id = :site AND state = 'COMMITTED' AND valid_to IS NULL AND scene_version <> :v
                """, params);
        jdbc.update("""
                UPDATE scene_version SET state = 'COMMITTED', committed_at = :now, valid_from = :now
                WHERE scene_version = :v AND state = 'CANDIDATE'
                """, params);
        jdbc.update("""
                INSERT INTO scene_activation (site_id, scene_version, previous_scene_version, activated_at, actor, reason)
                VALUES (:site, :v, :previous, :now, :actor, :reason)
                """, params);
    }

    /** 기존 API 와 CLI 의 전환을 직렬화한다. 빈 현재 Scene 에서의 동시 확정도 하나씩 처리한다. */
    void lock() {
        jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.scene.commit'))", Map.of());
    }

    private void requireAssets(Map<String, Object> row, MapSqlParameterSource params, boolean worker) {
        if (worker) {
            String raw = (String) row.get("output");
            if (raw == null) {
                throw new BusinessException(ErrorCode.Scene.SCENE_GLTF_MISSING);
            }
            JsonNode output = mapper.readTree(raw);
            for (JsonNode asset : output.path("assets")) {
                if (Set.of("GLTF_VISUAL", "SURFACE_SOURCE_MAP").contains(asset.path("kind").asString())) {
                    String uri = asset.path("uri").asString();
                    String prefix = storage.uri("");
                    if (!uri.startsWith(prefix)) {
                        throw new BusinessException(ErrorCode.Scene.SCENE_GLTF_MISSING);
                    }
                    requireSize(uri.substring(prefix.length()), asset.has("bytes") ? asset.path("bytes").asLong() : null);
                }
            }
        } else {
            List<Long> sizes = jdbc.queryForList("SELECT size_bytes FROM upload_grant WHERE object_key = :key AND purpose = 'SCENE_GLTF'",
                    params.addValue("key", row.get("gltf_key")), Long.class);
            if (sizes.isEmpty()) {
                throw new BusinessException(ErrorCode.Scene.SCENE_GLTF_MISSING);
            }
            requireSize((String) row.get("gltf_key"), sizes.getFirst());
        }
    }

    private void requireSize(String key, Long expected) {
        var size = storage.size(key);
        if (size.isEmpty() || (expected != null && size.getAsLong() != expected)) {
            throw new BusinessException(ErrorCode.Scene.SCENE_GLTF_MISSING);
        }
    }
}
