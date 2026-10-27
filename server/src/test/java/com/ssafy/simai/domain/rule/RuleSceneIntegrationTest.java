package com.ssafy.simai.domain.rule;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.scene.SceneService;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * 규칙 버전 조회(GET /api/rules)와 현재 Scene 조회(GET /api/scenes/current)(S15P31A101-264, 명세 7.1·12.6, FR-3·113).
 * 규칙 판은 R__rule_versions.sql 이 넣은 등록부 값이다. 시험은 지금보다 늦은 묶음·Scene 을 넣어 다른 행보다 앞서게 하고 지운다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = "simai.scene.default-gltf-key=scenes/demo/classroom.glb")
@ActiveProfiles("test")
class RuleSceneIntegrationTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    NamedParameterJdbcTemplate named;

    @Autowired
    ObjectStorage storage;

    @Autowired
    JwtEncoder jwtEncoder;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String calibration = "cal-264-" + tag;
    private final List<String> bundles = new ArrayList<>();
    private final List<String> scenes = new ArrayList<>();

    @AfterEach
    void cleanUp() {
        for (String bundle : bundles) {
            jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", bundle);
            jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", bundle);
        }
        for (String scene : scenes) {
            jdbc.update("DELETE FROM scene_version WHERE scene_version = ?", scene);
        }
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", calibration);
    }

    @Test
    void 규칙은_R1부터_R10까지_활성_여부와_현재_판_임계값과_모든_판의_적용_기간을_준다() throws Exception {
        String bundle = bundle("OPERATION");
        item(bundle, "R1", 1);
        item(bundle, "R2", 1);
        item(bundle, "R4", 1);

        JsonNode body = get("/api/rules", token("VIEWER"), 200);

        assertThat(body.get("rule_profile").asString()).isEqualTo("OPERATION");
        assertThat(body.get("config_version").asString()).isEqualTo(bundle);
        JsonNode rules = body.get("rules");
        assertThat(rules).hasSize(10);
        assertThat(rules.get(0).get("rule_id").asString()).isEqualTo("R1");
        assertThat(rules.get(0).get("name").asString()).isEqualTo("위험 구역 진입");
        assertThat(rules.get(0).get("active").asBoolean()).isTrue();
        JsonNode r1 = rules.get(0).get("current");
        assertThat(r1.get("rule_version").asInt()).isEqualTo(1);
        assertThat(r1.at("/thresholds/margin").asDouble()).isEqualTo(0.3);
        assertThat(r1.get("reads").get(0).asString()).isEqualTo("tracks.observation_state");
        assertThat(r1.get("valid_from_ms").isNumber()).isTrue();
        assertThat(r1.get("valid_to_ms").isNull()).isTrue();
        // 묶음에 없는 규칙은 비활성(등록부에 판이 있어도)
        assertThat(rules.get(2).get("rule_id").asString()).isEqualTo("R3");
        assertThat(rules.get(2).get("active").asBoolean()).isFalse();
        assertThat(rules.get(2).get("current").isNull()).isTrue();
        // R6 은 자기 판 없이 감싼 규칙(R1 의 thresholds.r6)으로 적용된다
        JsonNode r6 = rules.get(5);
        assertThat(r6.get("rule_id").asString()).isEqualTo("R6");
        assertThat(r6.get("active").asBoolean()).isTrue();
        assertThat(r6.get("applied_with").get(0).asString()).isEqualTo("R1");
        // 확장 규칙
        assertThat(rules.get(9).get("rule_id").asString()).isEqualTo("R10");
        assertThat(rules.get(9).get("stage").asString()).isEqualTo("EXTENSION");
        assertThat(rules.get(9).get("active").asBoolean()).isFalse();
        // 적용 기간: 교실 시험 판까지 모든 판, 값 없이
        JsonNode versions = body.get("versions");
        assertThat(versions.size()).isGreaterThanOrEqualTo(9);
        assertThat(versions.get(0).get("rule_id").asString()).isEqualTo("R1");
        assertThat(versions.get(0).has("thresholds")).isFalse();
        assertThat(versions.findValuesAsString("profile")).contains("CLASSROOM_TEST");
    }

    @Test
    void 교실_시험_묶음이면_그_묶음이_실은_교실_시험_판을_현재로_준다() throws Exception {
        String bundle = bundle("CLASSROOM_TEST");
        item(bundle, "R2", 1001);
        item(bundle, "R4", 1);

        JsonNode body = get("/api/rules", token("FOREMAN"), 200);

        assertThat(body.get("rule_profile").asString()).isEqualTo("CLASSROOM_TEST");
        JsonNode r2 = body.get("rules").get(1).get("current");
        assertThat(r2.get("rule_version").asInt()).isEqualTo(1001);
        assertThat(r2.get("profile").asString()).isEqualTo("CLASSROOM_TEST");
        assertThat(r2.at("/thresholds/profile/height_threshold_m").asDouble()).isEqualTo(0.35);
        // 감싼 규칙이 없으면 R6 도 비활성
        assertThat(body.get("rules").get(5).get("active").asBoolean()).isFalse();
    }

    @Test
    void 규칙과_Scene_은_로그인한_사용자만_본다() throws Exception {
        assertThat(http.send(HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/api/rules")).GET().build(),
                HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(401);
        assertThat(http.send(HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/api/scenes/current")).GET().build(),
                HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(401);
    }

    @Test
    void Scene_은_COMMITTED_가_있으면_그_판과_glTF_서명_주소를_없으면_등록한_기본_Scene_을_준다() throws Exception {
        String scene = "scene-264-" + tag;
        commitScene(scene, "scenes/" + tag + "/scene.glb");
        long before = Instant.now().toEpochMilli();

        JsonNode committed = get("/api/scenes/current", token("VIEWER"), 200);

        assertThat(committed.get("scene_version").asString()).isEqualTo(scene);
        assertThat(committed.get("state").asString()).isEqualTo("COMMITTED");
        assertThat(committed.get("default").asBoolean()).isFalse();
        assertThat(committed.get("calibration_version").asString()).isEqualTo(calibration);
        assertThat(committed.at("/quality/coverage").asDouble()).isEqualTo(0.8);
        assertThat(committed.get("gltf_url").asString()).startsWith("http://127.0.0.1:9000/simai-test-bucket/scenes/" + tag)
                .contains("X-Amz-Signature=");
        assertThat(committed.get("gltf_expires_at_ms").asLong()).isBetween(before + 290_000, before + 310_000);

        // 끝난(valid_to) Scene 은 현재가 아니다. COMMITTED 가 없으면 등록한 기본 Scene
        jdbc.update("UPDATE scene_version SET valid_to = now() WHERE scene_version = ?", scene);
        assertThat(openCommitted()).as("다른 시험이 남긴 COMMITTED Scene 이 없어야 한다").isZero();
        JsonNode fallback = get("/api/scenes/current", token("VIEWER"), 200);
        assertThat(fallback.get("state").asString()).isEqualTo("DEFAULT");
        assertThat(fallback.get("default").asBoolean()).isTrue();
        assertThat(fallback.get("scene_version").asString()).isEqualTo("scene-demo");
        assertThat(fallback.get("calibration_version").isNull()).isTrue();
        assertThat(fallback.get("gltf_url").asString()).contains("scenes/demo/classroom.glb");
    }

    @Test
    void 기본_Scene_도_등록하지_않았으면_SCENE_NOT_FOUND() {
        assertThat(openCommitted()).as("다른 시험이 남긴 COMMITTED Scene 이 없어야 한다").isZero();
        SceneService withoutDefault = new SceneService(named, storage, MAPPER, "scene-demo", "");

        assertThatThrownBy(withoutDefault::current).isInstanceOf(BusinessException.class)
                .hasFieldOrPropertyWithValue("errorCode", ErrorCode.Scene.SCENE_NOT_FOUND);
    }

    // ---- 도우미

    private int openCommitted() {
        return jdbc.queryForObject("SELECT count(*) FROM scene_version WHERE state = 'COMMITTED' AND valid_to IS NULL", Integer.class);
    }

    private void calibration() {
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, 'classroom-1', 'CALIBRATED', 'G1_AUTO', '시험', now(), 'test', 'sha256:test', now())
                ON CONFLICT DO NOTHING
                """, calibration);
    }

    /** 지금보다 늦게 발행한 묶음이라 가장 최근 묶음이 된다 */
    private String bundle(String profile) {
        calibration();
        String version = "cfg-264-" + tag + "-" + bundles.size();
        jdbc.update("""
                INSERT INTO config_bundle (config_version, site_id, issued_at, bundle_uri, bundle_hash, components, calibration_version,
                    rule_profile)
                VALUES (?, 'classroom-1', now() + interval '1 day', 'https://test/bundle', 'sha256:test', '{}'::jsonb, ?, ?)
                """, version, calibration, profile);
        bundles.add(version);
        return version;
    }

    private void item(String bundle, String ruleId, int ruleVersion) {
        jdbc.update("INSERT INTO config_bundle_item (config_version, item_type, item_id, item_version) VALUES (?, 'RULE', ?, ?)",
                bundle, ruleId, ruleVersion);
    }

    private void commitScene(String scene, String gltfKey) {
        calibration();
        jdbc.update("""
                INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, quality, is_upper_bound,
                    committed_at, valid_from)
                VALUES (?, 'classroom-1', ?, 'COMMITTED', ?, '{"coverage": 0.8}'::jsonb, false, now() + interval '1 day', now())
                """, scene, calibration, gltfKey);
        scenes.add(scene);
    }

    private JsonNode get(String path, String token, int expectedStatus) throws Exception {
        HttpResponse<String> response = http.send(HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token).GET().build(), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).as(path + " → " + response.body()).isEqualTo(expectedStatus);
        return MAPPER.readTree(response.body());
    }

    private String token(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject("1").issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }
}
