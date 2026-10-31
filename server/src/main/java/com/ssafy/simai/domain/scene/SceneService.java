package com.ssafy.simai.domain.scene;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현재 Scene 조회(GET /api/scenes/current, S15P31A101-264, 명세 3.7·4.5·12.6, FR-3). DB 활성 이력이 고른 COMMITTED
 * Scene 과 glTF 의 짧게 사는 GET 서명 주소를 준다(viewer_health.scene.committed_version 과 같은 선택, ViewerHealthService).
 * Scene 생성(S15P31A101-137, M3) 전에는 설정으로 등록한 시연용 기본 Scene(simai.scene.default-gltf-key)을 default=true 로 준다.
 * 기본 Scene 을 DB 행으로 두지 않는 것은 scene_version 이 보정 버전(FK)을 요구하기 때문이다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SceneService {

    /**
     * 현재 Scene: 현장별 마지막 활성화 버전(이력이 없으면 기존 COMMITTED 버전)(현장별 한 행, scene_version 표의 열 그대로). 관제 상태의 Scene 불일치(FR-12,
     * ViewerHealthService)와 이 조회가 같은 Scene 을 가리키도록 이 질의 하나를 쓴다.
     */
    private static final Logger log = LoggerFactory.getLogger(SceneService.class);

    public static final String CURRENT_COMMITTED = """
            SELECT s.* FROM scene_version s
            WHERE s.scene_version = COALESCE(
                (SELECT a.scene_version FROM scene_activation a WHERE a.site_id = s.site_id ORDER BY a.activation_id DESC LIMIT 1),
                (SELECT legacy.scene_version FROM scene_version legacy
                 WHERE legacy.site_id = s.site_id AND legacy.state = 'COMMITTED' AND legacy.valid_to IS NULL
                 ORDER BY legacy.committed_at DESC NULLS LAST, legacy.scene_version LIMIT 1))
            AND s.state = 'COMMITTED'
            ORDER BY s.committed_at DESC NULLS LAST
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectStorage storage;
    private final ObjectMapper mapper;
    private final String defaultVersion;
    private final String defaultGltfKey;

    public SceneService(NamedParameterJdbcTemplate jdbc, ObjectStorage storage, ObjectMapper mapper,
            @Value("${simai.scene.default-version:scene-demo}") String defaultVersion,
            @Value("${simai.scene.default-gltf-key:}") String defaultGltfKey) {
        this.jdbc = jdbc;
        this.storage = storage;
        this.mapper = mapper;
        this.defaultVersion = defaultVersion;
        this.defaultGltfKey = defaultGltfKey.strip();
    }

    public ObjectNode current() {
        List<Committed> committed = jdbc.query("""
                SELECT s.scene_version, s.calibration_version, c.state AS calibration_state, s.gltf_key,
                    s.quality::text AS quality, s.scene_output::text AS output, s.committed_at
                FROM (""" + CURRENT_COMMITTED + """
                ) s
                LEFT JOIN calibration_version c ON c.calibration_version = s.calibration_version AND c.site_id = s.site_id
                LIMIT 1
                """, Map.of(), (rs, i) -> new Committed(rs.getString("scene_version"), rs.getString("calibration_version"),
                rs.getString("calibration_state"), rs.getString("gltf_key"), rs.getString("quality"), rs.getString("output"),
                rs.getObject("committed_at", OffsetDateTime.class)));
        ObjectNode node = mapper.createObjectNode();
        if (!committed.isEmpty()) {
            Committed s = committed.getFirst();
            node.put("scene_version", s.version());
            node.put("state", "COMMITTED");
            node.put("default", false);
            node.put("calibration_version", s.calibrationVersion());
            if (s.calibrationState() != null && !s.calibrationState().isBlank()) {
                node.put("calibration_state", s.calibrationState());
            }
            node.put("committed_at_ms", s.committedAt() == null ? null : s.committedAt().toInstant().toEpochMilli());
            node.set("quality", s.quality() == null ? mapper.nullNode() : mapper.readTree(s.quality()));
            gltf(node, s.gltfKey());
            sourceMaps(node, s.output());
            return node;
        }
        if (defaultGltfKey.isEmpty()) {
            throw new BusinessException(ErrorCode.Scene.SCENE_NOT_FOUND);
        }
        node.put("scene_version", defaultVersion);
        node.put("state", "DEFAULT");
        // 시연용 기본 배경. 현장을 복원한 Scene 이 아니라 보정·품질 근거가 없다(FR-3 "그 전에는")
        node.put("default", true);
        node.putNull("calibration_version");
        node.putNull("committed_at_ms");
        node.putNull("quality");
        gltf(node, defaultGltfKey);
        return node;
    }

    private record Committed(String version, String calibrationVersion, String calibrationState, String gltfKey, String quality,
            String output, OffsetDateTime committedAt) {
    }

    /** 재질 순서대로 내려준다. 한 장이라도 서명하지 못하면 부분 배열을 보내 순서를 틀리지 않는다. */
    private void sourceMaps(ObjectNode node, String output) {
        if (output == null || !storage.configured()) {
            return;
        }
        var urls = mapper.createArrayNode();
        try {
            for (JsonNode asset : mapper.readTree(output).path("assets")) {
                if ("SURFACE_SOURCE_MAP".equals(asset.path("kind").asString())) {
                    String uri = asset.path("uri").asString();
                    String prefix = storage.uri("");
                    if (!uri.startsWith(prefix) || uri.length() == prefix.length()) {
                        return;
                    }
                    urls.add(storage.signGet(uri.substring(prefix.length())).url());
                }
            }
        } catch (BusinessException e) {
            log.warn("Scene 출처 지도 서명 실패: {}", e.getErrorCode());
            return;
        }
        if (!urls.isEmpty()) {
            node.set("surface_source_map_urls", urls);
        }
    }

    /**
     * glTF 서명 주소(5분). 키가 없거나 저장소를 쓸 수 없으면(미설정·자격 증명 없음) 주소만 비우고 Scene 정보는 준다.
     * 화면은 배경 없이도 Scene 버전·불일치를 보여야 한다. 서명 주소는 로그에 남기지 않는다.
     */
    private void gltf(ObjectNode node, String key) {
        node.putNull("gltf_url");
        node.putNull("gltf_expires_at_ms");
        if (key == null || key.isBlank() || !storage.configured()) {
            return;
        }
        SignedRequest signed;
        try {
            signed = storage.signGet(key);
        } catch (BusinessException e) {
            log.warn("Scene glTF 서명 주소를 만들지 못해 주소 없이 준다: {}", e.getErrorCode());
            return;
        }
        node.put("gltf_url", signed.url());
        node.put("gltf_expires_at_ms", signed.expiresAt().toEpochMilli());
    }
}
