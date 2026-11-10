package com.ssafy.simai.domain.scene;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * Scene 입력 사진 서명 주소(S15P31A101-592) DB 경로: scene_version.job_id → analysis_job.input(묶음·images) 연결, 작업 없는 Scene 은 빈 목록,
 * 없는 Scene 404, 열람 권한. 응답은 contracts/ scene_camera_images 로 검사한다. 저장소는 가짜(서명은 키를 담은 고정 주소).
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class SceneCameraImageIntegrationTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ContractSchemas schemas;

    @MockitoBean
    ObjectStorage storage;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String bundle = "bundle-" + UUID.randomUUID().toString().replace("-", "");
    private final String calibration = "cal-592-" + tag;
    private final String jobId = "job-592-" + tag;
    private final String baked = "scene-592-baked-" + tag;
    private final String manual = "scene-592-manual-" + tag;

    @BeforeEach
    void setUp() {
        when(storage.configured()).thenReturn(true);
        when(storage.uri(anyString())).thenAnswer(i -> "s3://simai-test/" + i.getArgument(0));
        when(storage.signGet(anyString())).thenAnswer(i -> new SignedRequest("https://storage.test/" + i.getArgument(0), Map.of(),
                Instant.now().plusSeconds(300)));
        String site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'CALIBRATED', 'G1_AUTO', '시험', now(), 'test', 'sha256:test', now())
                """, calibration, site);
        String prefix = "s3://simai-test/static-inputs/" + bundle + "/";
        Map<String, Object> input = Map.of("calibration_version", calibration, "clean_background_bundle_id", bundle,
                "images", List.of(
                        Map.of("camera_id", "cam_B", "uri", prefix + "cam_B.jpg", "kind", "CLEAN_BACKGROUND"),
                        Map.of("camera_id", "cam_A", "uri", prefix + "cam_A.jpg", "kind", "CLEAN_BACKGROUND")));
        jdbc.update("""
                INSERT INTO analysis_job (job_id, job_type, priority, status, attempt, max_attempts, timeout_ms, input, created_at)
                VALUES (?, 'static_reconstruct', 'BACKGROUND', 'SUCCEEDED', 1, 2, 600000, CAST(? AS jsonb), now())
                """, jobId, MAPPER.writeValueAsString(input));
        jdbc.update("""
                INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, is_upper_bound, job_id)
                VALUES (?, ?, ?, 'CANDIDATE', 'static-3d/x/scene.glb', false, ?), (?, ?, ?, 'CANDIDATE', 'scenes/manual.glb', false, NULL)
                """, baked, site, calibration, jobId, manual, site, calibration);
    }

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM scene_version WHERE scene_version IN (?, ?)", baked, manual);
        jdbc.update("DELETE FROM analysis_job WHERE job_id = ?", jobId);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", calibration);
    }

    @Test
    void 작업으로_구운_Scene은_그_묶음_사진을_주고_작업이_없으면_비운다() throws Exception {
        JsonNode body = get("/api/scenes/" + baked + "/camera-images", token("VIEWER"), 200);
        assertThat(body.path("bundle_id").asString()).isEqualTo(bundle);
        assertThat(body.path("images")).hasSize(2);
        assertThat(body.path("images").get(0).path("camera_id").asString()).isEqualTo("cam_A");
        assertThat(body.path("images").get(0).path("url").asString())
                .isEqualTo("https://storage.test/static-inputs/" + bundle + "/cam_A.jpg");
        assertThat(body.path("expires_at_ms").isNumber()).isTrue();
        assertThat(schemas.violations("messages/scene_camera_images", body.toString())).isEmpty();

        JsonNode unknown = get("/api/scenes/" + manual + "/camera-images", token("VIEWER"), 200);
        assertThat(unknown.path("bundle_id").isNull()).isTrue();
        assertThat(unknown.path("images")).isEmpty();
        assertThat(schemas.violations("messages/scene_camera_images", unknown.toString())).isEmpty();

        assertThat(get("/api/scenes/scene-592-missing-" + tag + "/camera-images", token("VIEWER"), 404).path("code").asString())
                .isEqualTo("SCENE_VERSION_NOT_FOUND");
        get("/api/scenes/" + baked + "/camera-images", null, 401);
    }

    private JsonNode get(String path, String token, int expectedStatus) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path)).GET();
        if (token != null) {
            request.header(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        HttpResponse<String> response = http.send(request.build(), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).as("GET " + path + " → " + response.body()).isEqualTo(expectedStatus);
        return response.body().isEmpty() ? MAPPER.nullNode() : MAPPER.readTree(response.body());
    }

    private String token(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject("1").issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }
}
