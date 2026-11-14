package com.ssafy.simai.domain.gpu;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.scene.SceneService;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.properties.ConfigBundleProperties;
import java.util.List;
import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 등록 응답의 start(계약 gpu_session_register_result $defs/start, S15P31A101-699). 지금 Lead 가 손으로 만드는 시작 묶음
 * (calibration.json·laptop_token.sha256)과 simai-bootstrap lobby 인자(--ec2-base-url·--camera-ids·--measured-heights·
 * --scene-version·--config-version)를 EC2 가 아는 값으로 채운다. 모르는 값은 비운다(AGENTS 규칙 2).
 * 보정 문서는 새로 만들지 않고 가장 최근 구성 묶음의 서명 주소를 그대로 준다(Tier 0 가 이미 받는 방식, BundleUrlSigner).
 * TLS 비밀 키는 EC2 에 없다. 토큰을 발급하기 전에 만든다(여기서 실패하면 이전 세션을 건드리지 않는다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class GpuStartConfig {

    private final NamedParameterJdbcTemplate jdbc;
    private final SiteLookup site;
    private final ConfigBundleProperties bundle;
    private final ObjectMapper mapper;

    GpuStartConfig(NamedParameterJdbcTemplate jdbc, SiteLookup site, ConfigBundleProperties bundle, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.site = site;
        this.bundle = bundle;
        this.mapper = mapper;
    }

    ObjectNode build() {
        // Tier 0 는 https EC2 만 받는다(config_fetch.py). 없으면 기동 설정을 줄 수 없어 등록을 받지 않는다
        if (!bundle.publicBaseUrl().startsWith("https://")) {
            throw new BusinessException(ErrorCode.Gpu.EC2_BASE_URL_MISSING);
        }
        String siteId = site.siteId();
        MapSqlParameterSource siteParam = new MapSqlParameterSource("site", siteId);
        ObjectNode start = mapper.createObjectNode();
        start.put("ec2_base_url", bundle.publicBaseUrl());
        start.put("site_id", siteId);

        List<String> cameras = jdbc.queryForList("""
                SELECT camera_id FROM camera WHERE site_id = :site AND removed_at IS NULL ORDER BY camera_id
                """, siteParam, String.class);
        ArrayNode cameraIds = start.putArray("camera_ids");
        cameras.forEach(cameraIds::add);

        // 세팅 화면이 저장한 카메라별 마지막 줄자 높이(SetupService). 지금 설치된 카메라의 양수 값만 싣는다
        String heightsJson = jdbc.queryForObject("SELECT default_camera_heights_m::text FROM site WHERE site_id = :site",
                siteParam, String.class);
        ObjectNode heights = start.putObject("measured_heights_m");
        if (heightsJson != null) {
            JsonNode stored = mapper.readTree(heightsJson);
            for (String cam : cameras) {
                JsonNode value = stored.get(cam);
                if (value != null && value.isNumber() && value.doubleValue() > 0 && value.doubleValue() <= 20) {
                    heights.put(cam, value.doubleValue());
                }
            }
        }

        // device.token_hash 는 기기 토큰 원문 SHA-256 16진수(TokenHashes)다. Tier 0 는 sha256:<hex> 목록으로 받는다
        ArrayNode laptops = start.putArray("laptop_token_sha256");
        jdbc.queryForList("""
                SELECT token_hash FROM device WHERE kind = 'LAPTOP' AND revoked_at IS NULL ORDER BY device_id
                """, Map.of(), String.class).forEach(hash -> laptops.add("sha256:" + hash));

        List<String> scenes = jdbc.queryForList("SELECT c.scene_version FROM (" + SceneService.CURRENT_COMMITTED + ") c LIMIT 1",
                Map.of(), String.class);
        start.put("scene_version", scenes.isEmpty() ? null : scenes.getFirst());

        List<ObjectNode> configs = jdbc.query("""
                SELECT config_version, bundle_uri, bundle_hash, calibration_version, components::text AS components
                FROM config_bundle WHERE site_id = :site ORDER BY issued_at DESC LIMIT 1
                """, siteParam, (rs, i) -> config(rs.getString("config_version"), rs.getString("bundle_uri"),
                rs.getString("bundle_hash"), mapper.readTree(rs.getString("components"))));
        if (configs.isEmpty() || configs.getFirst() == null) {
            start.putNull("config");
        } else {
            start.set("config", configs.getFirst());
        }
        return start;
    }

    /** 매니페스트·보정 구성 요소의 서명 주소와 해시. 주소가 https 가 아니면(공개 주소 설정 전 묶음) 쓸 수 없어 비운다 */
    private ObjectNode config(String version, String manifestUri, String manifestHash, JsonNode components) {
        JsonNode calibration = components.get("calibration");
        if (calibration == null || !https(manifestUri) || !https(text(calibration, "uri"))
                || text(calibration, "version") == null || text(calibration, "sha256") == null) {
            return null;
        }
        ObjectNode config = mapper.createObjectNode();
        config.put("config_version", version);
        ObjectNode manifest = config.putObject("manifest");
        manifest.put("uri", manifestUri);
        manifest.put("sha256", manifestHash);
        ObjectNode cal = config.putObject("calibration");
        cal.put("version", text(calibration, "version"));
        cal.put("uri", text(calibration, "uri"));
        cal.put("sha256", text(calibration, "sha256"));
        return config;
    }

    private static boolean https(String uri) {
        return uri != null && uri.startsWith("https://");
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.get(field);
        return value == null || !value.isString() ? null : value.stringValue();
    }
}
