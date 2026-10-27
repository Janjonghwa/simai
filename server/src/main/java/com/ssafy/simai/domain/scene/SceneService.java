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
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현재 Scene 조회(GET /api/scenes/current, S15P31A101-264, 명세 3.7·4.5·12.6, FR-3). COMMITTED 이고 끝나지 않은(valid_to 없음)
 * 가장 최근 Scene 과 glTF 의 짧게 사는 GET 서명 주소를 준다(viewer_health.scene.committed_version 과 같은 선택, ViewerHealthService).
 * Scene 생성(S15P31A101-137, M3) 전에는 설정으로 등록한 시연용 기본 Scene(simai.scene.default-gltf-key)을 default=true 로 준다.
 * 기본 Scene 을 DB 행으로 두지 않는 것은 scene_version 이 보정 버전(FK, CALIBRATED 에서만 생성 4.5)을 요구하기 때문이다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SceneService {

    /**
     * 현재 Scene: COMMITTED 이고 끝나지 않은 가장 최근 것(한 행, scene_version 표의 열 그대로). 관제 상태의 Scene 불일치(FR-12,
     * ViewerHealthService)와 이 조회가 같은 Scene 을 가리키도록 이 질의 하나를 쓴다.
     */
    private static final Logger log = LoggerFactory.getLogger(SceneService.class);

    public static final String CURRENT_COMMITTED = """
            SELECT * FROM scene_version WHERE state = 'COMMITTED' AND valid_to IS NULL ORDER BY committed_at DESC NULLS LAST LIMIT 1
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
                SELECT scene_version, calibration_version, gltf_key, quality::text AS quality, committed_at FROM (
                """ + CURRENT_COMMITTED + ") s", Map.of(), (rs, i) -> new Committed(rs.getString("scene_version"), rs.getString("calibration_version"),
                rs.getString("gltf_key"), rs.getString("quality"), rs.getObject("committed_at", OffsetDateTime.class)));
        ObjectNode node = mapper.createObjectNode();
        if (!committed.isEmpty()) {
            Committed s = committed.getFirst();
            node.put("scene_version", s.version());
            node.put("state", "COMMITTED");
            node.put("default", false);
            node.put("calibration_version", s.calibrationVersion());
            node.put("committed_at_ms", s.committedAt() == null ? null : s.committedAt().toInstant().toEpochMilli());
            node.set("quality", s.quality() == null ? mapper.nullNode() : mapper.readTree(s.quality()));
            gltf(node, s.gltfKey());
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

    private record Committed(String version, String calibrationVersion, String gltfKey, String quality, OffsetDateTime committedAt) {
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
