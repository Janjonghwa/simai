package com.ssafy.simai.domain.site;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.OptionalLong;
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
import tools.jackson.databind.node.ObjectNode;

/**
 * 현장 등록·수정과 Scene 등록·확정(S15P31A101-322). 실제 포트로 관리자 JWT 를 실어 부른다. 현장 표는 모든 시험이 함께 쓰는 한 행이라
 * 고친 값은 시험마다 되돌린다. Scene·보정·업로드 발급은 이 시험만의 이름으로 만들고 지운다.
 * 저장소는 가짜다: 서명은 고정 주소를 주고, 확정 전 객체 확인(HEAD)은 기본으로 발급 크기(1000)를 돌려준다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class SiteSceneIntegrationTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final String SHA = "c".repeat(64);

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @MockitoBean
    ObjectStorage storage;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final List<String> scenes = new ArrayList<>();
    private final List<String> keys = new ArrayList<>();
    private long adminId;
    private String siteId;
    private String siteName;

    @BeforeEach
    void setUp() {
        SignedRequest signed = new SignedRequest("https://storage.test/signed", Map.of(), Instant.now().plusSeconds(300));
        when(storage.configured()).thenReturn(true);
        when(storage.signPut(anyString(), anyString(), anyLong(), anyString())).thenReturn(signed);
        when(storage.signGet(anyString())).thenReturn(signed);
        when(storage.uri(anyString())).thenAnswer(i -> "s3://simai-test/" + i.getArgument(0));
        when(storage.size(anyString())).thenReturn(OptionalLong.of(1000));
        adminId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id", Long.class, "site-admin-" + tag);
        siteId = jdbc.queryForObject("SELECT site_id FROM site", String.class);
        siteName = jdbc.queryForObject("SELECT name FROM site", String.class);
        calibration("cal-" + tag, "PROVISIONAL");
        calibration("cal-un-" + tag, "UNCALIBRATED");
    }

    @AfterEach
    void cleanUp() {
        for (String scene : scenes) {
            jdbc.update("DELETE FROM scene_version WHERE scene_version = ?", scene);
        }
        for (String key : keys) {
            jdbc.update("DELETE FROM upload_grant WHERE object_key = ?", key);
        }
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version IN (?, ?)", "cal-" + tag, "cal-un-" + tag);
        jdbc.update("UPDATE site SET name = ?, world_origin = NULL", siteName);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 현장을_조회하고_관리자가_이름과_원점_근거를_고친다() throws Exception {
        JsonNode current = call("GET", "/api/sites/current", token("VIEWER"), null, 200);
        assertThat(current.get("site_id").asString()).isEqualTo(siteId);

        ObjectNode body = MAPPER.createObjectNode().put("name", "운영 현장 " + tag);
        body.putObject("world_origin").put("source", "WALLS");
        JsonNode updated = call("PUT", "/api/sites/current", admin(), body, 200);
        assertThat(updated.get("name").asString()).isEqualTo("운영 현장 " + tag);
        assertThat(updated.at("/world_origin/source").asString()).isEqualTo("WALLS");
        // 보내지 않은 칸은 그대로, null 은 비운다
        ObjectNode clear = MAPPER.createObjectNode();
        clear.putNull("world_origin");
        JsonNode cleared = call("PUT", "/api/sites/current", admin(), clear, 200);
        assertThat(cleared.get("name").asString()).isEqualTo("운영 현장 " + tag);
        assertThat(cleared.get("world_origin").isNull()).isTrue();

        // 현장 하나(MVP): 이미 있으면 등록하지 않는다
        ObjectNode register = MAPPER.createObjectNode().put("site_id", "site-" + tag).put("name", "둘째");
        assertThat(call("POST", "/api/sites", admin(), register, 409).get("code").asString()).isEqualTo("SITE_EXISTS");
        call("PUT", "/api/sites/current", token("FOREMAN"), body, 403);
        call("POST", "/api/sites", token("VIEWER"), register, 403);
        for (JsonNode bad : List.of(MAPPER.createObjectNode(), MAPPER.createObjectNode().put("foo", 1),
                MAPPER.createObjectNode().put("site_id", "Bad_Id"),
                MAPPER.createObjectNode().put("name", " "), MAPPER.createObjectNode().put("world_origin", "x"))) {
            assertThat(call("PUT", "/api/sites/current", admin(), bad, 400).get("code").asString()).isEqualTo("INVALID_SITE");
        }
    }

    @Test
    void 기록이_붙은_현장은_site_id_를_바꾸지_않고_비었으면_카메라와_함께_옮긴다() throws Exception {
        // 이 시험의 보정 버전이 붙어 있다(setUp)
        ObjectNode rename = MAPPER.createObjectNode().put("site_id", "site-" + tag);
        assertThat(call("PUT", "/api/sites/current", admin(), rename, 409).get("code").asString()).isEqualTo("SITE_IN_USE");
        assertThat(jdbc.queryForObject("SELECT site_id FROM site", String.class)).isEqualTo(siteId);

        // 다른 시험이 남긴 기록이 없을 때만 옮기는 경로를 본다(현장 표는 모두가 함께 쓴다)
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version IN (?, ?)", "cal-" + tag, "cal-un-" + tag);
        boolean empty = jdbc.queryForObject("""
                SELECT NOT EXISTS (SELECT 1 FROM zone) AND NOT EXISTS (SELECT 1 FROM static_object)
                    AND NOT EXISTS (SELECT 1 FROM config_bundle) AND NOT EXISTS (SELECT 1 FROM calibration_version)
                    AND NOT EXISTS (SELECT 1 FROM calibration_run) AND NOT EXISTS (SELECT 1 FROM scale_reference)
                    AND NOT EXISTS (SELECT 1 FROM floor_plan) AND NOT EXISTS (SELECT 1 FROM scene_version)
                    AND NOT EXISTS (SELECT 1 FROM bev_frame_set)
                    AND NOT EXISTS (SELECT 1 FROM watch_display_name_history) AND NOT EXISTS (SELECT 1 FROM person_world_name_history)
                    AND NOT EXISTS (SELECT 1 FROM person_unidentified_number_history) AND NOT EXISTS (SELECT 1 FROM person_world_merge_log)
                """, Boolean.class);
        assumeTrue(empty, "다른 시험의 현장 기록이 남아 있어 site_id 바꾸기는 건너뜀");
        int cameras = jdbc.queryForObject("SELECT count(*) FROM camera WHERE site_id = ?", Integer.class, siteId);
        try {
            JsonNode moved = call("PUT", "/api/sites/current", admin(), rename, 200);
            assertThat(moved.get("site_id").asString()).isEqualTo("site-" + tag);
            assertThat(jdbc.queryForObject("SELECT count(*) FROM camera WHERE site_id = ?", Integer.class, "site-" + tag))
                    .isEqualTo(cameras);
        } finally {
            call("PUT", "/api/sites/current", admin(), MAPPER.createObjectNode().put("site_id", siteId), 200);
        }
        assertThat(jdbc.queryForObject("SELECT site_id FROM site", String.class)).isEqualTo(siteId);
    }

    @Test
    void 고정_목록에_없는_FK_표_일일_일지가_있어도_site_id_를_바꾸지_않고_409_로_답한다() throws Exception {
        // 고정 목록(DEPENDENTS)에 없던 report(V36 site FK)만 남긴다. 예전에는 FK 위반 500 이었다(S15P31A101-632)
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version IN (?, ?)", "cal-" + tag, "cal-un-" + tag);
        boolean empty = jdbc.queryForObject("""
                SELECT NOT EXISTS (SELECT 1 FROM zone) AND NOT EXISTS (SELECT 1 FROM static_object)
                    AND NOT EXISTS (SELECT 1 FROM config_bundle) AND NOT EXISTS (SELECT 1 FROM calibration_version)
                    AND NOT EXISTS (SELECT 1 FROM calibration_run) AND NOT EXISTS (SELECT 1 FROM scale_reference)
                    AND NOT EXISTS (SELECT 1 FROM floor_plan) AND NOT EXISTS (SELECT 1 FROM scene_version)
                    AND NOT EXISTS (SELECT 1 FROM bev_frame_set) AND NOT EXISTS (SELECT 1 FROM work_unit)
                    AND NOT EXISTS (SELECT 1 FROM layout_plan) AND NOT EXISTS (SELECT 1 FROM setup_session)
                    AND NOT EXISTS (SELECT 1 FROM watch_display_name_history) AND NOT EXISTS (SELECT 1 FROM person_world_name_history)
                    AND NOT EXISTS (SELECT 1 FROM person_unidentified_number_history) AND NOT EXISTS (SELECT 1 FROM person_world_merge_log)
                """, Boolean.class);
        assumeTrue(empty, "다른 시험의 현장 기록이 남아 있어 report 만 붙은 경우를 만들 수 없어 건너뜀");
        Long reportId = jdbc.queryForObject("""
                INSERT INTO report (report_kind, site_id, report_date, report_version, status)
                VALUES ('DAILY', ?, DATE '2001-01-01', 1, 'SUCCEEDED') RETURNING report_id
                """, Long.class, siteId);
        try {
            ObjectNode rename = MAPPER.createObjectNode().put("site_id", "site-" + tag);
            assertThat(call("PUT", "/api/sites/current", admin(), rename, 409).get("code").asString()).isEqualTo("SITE_IN_USE");
            assertThat(jdbc.queryForObject("SELECT site_id FROM site", String.class)).isEqualTo(siteId);
        } finally {
            jdbc.update("DELETE FROM report WHERE report_id = ?", reportId);
        }
    }

    @Test
    void 관리자가_올린_glb_를_Scene_으로_등록하고_확정하면_현재_Scene_이_된다() throws Exception {
        String first = "scene-a-" + tag;
        String second = "scene-b-" + tag;
        JsonNode created = call("POST", "/api/scenes", admin(), scene(first, "cal-" + tag, upload()), 201);
        assertThat(created.get("state").asString()).isEqualTo("CANDIDATE");
        assertThat(created.get("site_id").asString()).isEqualTo(siteId);
        assertThat(created.get("requested_by").asLong()).isEqualTo(adminId);
        assertThat(created.has("gltf_key")).isFalse();

        JsonNode committed = call("POST", "/api/scenes/" + first + "/commit", admin(), null, 200);
        assertThat(committed.get("state").asString()).isEqualTo("COMMITTED");
        assertThat(committed.get("valid_to_ms").isNull()).isTrue();
        JsonNode current = call("GET", "/api/scenes/current", token("VIEWER"), null, 200);
        assertThat(current.get("scene_version").asString()).isEqualTo(first);
        assertThat(current.get("default").asBoolean()).isFalse();

        // 다음 Scene 을 확정하면 앞 Scene 은 valid_to 로 닫힌다(기록은 남는다)
        call("POST", "/api/scenes", admin(), scene(second, "cal-" + tag, upload()), 201);
        call("POST", "/api/scenes/" + second + "/commit", admin(), null, 200);
        assertThat(call("GET", "/api/scenes/" + first, admin(), null, 200).get("valid_to_ms").isNull()).isFalse();
        assertThat(call("GET", "/api/scenes/current", admin(), null, 200).get("scene_version").asString()).isEqualTo(second);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM scene_version WHERE state = 'COMMITTED' AND valid_to IS NULL",
                Integer.class)).isOne();
        // 확정된 Scene 은 다시 확정·거절하지 않는다
        assertThat(call("POST", "/api/scenes/" + first + "/commit", admin(), null, 409).get("code").asString())
                .isEqualTo("SCENE_STATE_CONFLICT");
    }

    @Test
    void 거절과_등록_검증() throws Exception {
        String rejected = "scene-r-" + tag;
        String key = upload();
        call("POST", "/api/scenes", admin(), scene(rejected, "cal-" + tag, key), 201);
        // 같은 glb·같은 버전으로 두 번 만들지 않는다
        assertThat(call("POST", "/api/scenes", admin(), scene("scene-x-" + tag, "cal-" + tag, key), 400).get("code").asString())
                .isEqualTo("SCENE_GLTF_NOT_UPLOADED");
        assertThat(call("POST", "/api/scenes", admin(), scene(rejected, "cal-" + tag, upload()), 409).get("code").asString())
                .isEqualTo("SCENE_EXISTS");
        JsonNode done = call("POST", "/api/scenes/" + rejected + "/reject", admin(),
                MAPPER.createObjectNode().put("reason", "축척이 맞지 않음"), 200);
        assertThat(done.get("state").asString()).isEqualTo("REJECTED");
        assertThat(done.get("rejected_reason").asString()).isEqualTo("축척이 맞지 않음");
        call("POST", "/api/scenes/" + rejected + "/commit", admin(), null, 409);

        // 확정 직전에 다시 본다: glb 가 없거나 크기가 다르면, 등록 뒤 보정이 DRIFTED 가 됐으면 확정하지 않는다
        String missing = "scene-m-" + tag;
        String missingKey = upload();
        call("POST", "/api/scenes", admin(), scene(missing, "cal-" + tag, missingKey), 201);
        when(storage.size(missingKey)).thenReturn(OptionalLong.empty());
        assertThat(call("POST", "/api/scenes/" + missing + "/commit", admin(), null, 409).get("code").asString())
                .isEqualTo("SCENE_GLTF_MISSING");
        when(storage.size(missingKey)).thenReturn(OptionalLong.of(999));
        call("POST", "/api/scenes/" + missing + "/commit", admin(), null, 409);
        when(storage.size(missingKey)).thenReturn(OptionalLong.of(1000));
        jdbc.update("UPDATE calibration_version SET state = 'DRIFTED' WHERE calibration_version = ?", "cal-" + tag);
        assertThat(call("POST", "/api/scenes/" + missing + "/commit", admin(), null, 409).get("code").asString())
                .isEqualTo("SCENE_CALIBRATION_INVALID");
        assertThat(call("GET", "/api/scenes/" + missing, admin(), null, 200).get("state").asString()).isEqualTo("CANDIDATE");
        jdbc.update("UPDATE calibration_version SET state = 'PROVISIONAL' WHERE calibration_version = ?", "cal-" + tag);

        // 발급받지 않은 키, UNCALIBRATED·없는 보정, 잘못된 버전, 권한
        assertThat(call("POST", "/api/scenes", admin(), scene("scene-y-" + tag, "cal-" + tag, "scenes/none.glb"), 400)
                .get("code").asString()).isEqualTo("SCENE_GLTF_NOT_UPLOADED");
        assertThat(call("POST", "/api/scenes", admin(), scene("scene-y-" + tag, "cal-un-" + tag, upload()), 409)
                .get("code").asString()).isEqualTo("SCENE_CALIBRATION_INVALID");
        assertThat(call("POST", "/api/scenes", admin(), scene("scene-y-" + tag, "cal-none-" + tag, upload()), 409)
                .get("code").asString()).isEqualTo("SCENE_CALIBRATION_INVALID");
        for (String version : List.of("current", "Bad", "")) {
            assertThat(call("POST", "/api/scenes", admin(), scene(version, "cal-" + tag, "k"), 400).get("code").asString())
                    .isEqualTo("INVALID_SCENE");
        }
        call("POST", "/api/scenes/" + rejected + "/reject", admin(), MAPPER.createObjectNode(), 400);
        assertThat(call("GET", "/api/scenes/none-" + tag, admin(), null, 404).get("code").asString())
                .isEqualTo("SCENE_VERSION_NOT_FOUND");
        call("POST", "/api/scenes", token("FOREMAN"), scene("scene-z-" + tag, "cal-" + tag, "k"), 403);
        call("GET", "/api/scenes/" + rejected, token("VIEWER"), null, 403);
        // Scene glTF 업로드는 관리자만, 조치 사진은 여전히 반장만
        ObjectNode sign = MAPPER.createObjectNode().put("purpose", "SCENE_GLTF").put("content_type", "model/gltf-binary")
                .put("size_bytes", 10).put("sha256", SHA);
        call("POST", "/api/uploads/sign", token("FOREMAN"), sign, 403);
        ObjectNode photo = MAPPER.createObjectNode().put("purpose", "TICKET_PHOTO").put("content_type", "image/jpeg")
                .put("size_bytes", 10).put("sha256", SHA);
        call("POST", "/api/uploads/sign", admin(), photo, 403);
    }

    /** 관리자 서명으로 받은 glb 키(실제로 올리지는 않는다). 발급 기록은 requester USER·requested_by_user 관리자 */
    private String upload() throws Exception {
        ObjectNode sign = MAPPER.createObjectNode().put("purpose", "SCENE_GLTF").put("content_type", "model/gltf-binary")
                .put("size_bytes", 1000).put("sha256", SHA);
        String key = call("POST", "/api/uploads/sign", admin(), sign, 200).get("object_key").asString();
        keys.add(key);
        assertThat(key).startsWith("scenes/").endsWith(".glb");
        assertThat(jdbc.queryForObject("SELECT requester || ':' || requested_by_user FROM upload_grant WHERE object_key = ?",
                String.class, key)).isEqualTo("USER:" + adminId);
        return key;
    }

    private ObjectNode scene(String version, String calibration, String key) {
        scenes.add(version);
        ObjectNode body = MAPPER.createObjectNode().put("scene_version", version).put("calibration_version", calibration)
                .put("gltf_key", key);
        body.putObject("quality").put("scale_error_m", 0.04);
        return body;
    }

    private void calibration(String version, String state) {
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, ?, 'G1_AUTO', '시험', now(), 'test', 'sha256:test', now())
                """, version, siteId, state);
    }

    private JsonNode call(String method, String path, String token, JsonNode body, int expectedStatus) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .header(HttpHeaders.CONTENT_TYPE, "application/json")
                .method(method, body == null ? HttpRequest.BodyPublishers.noBody()
                        : HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)));
        HttpResponse<String> response = http.send(request.build(), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).as(method + " " + path + " → " + response.body()).isEqualTo(expectedStatus);
        return response.body().isEmpty() ? MAPPER.nullNode() : MAPPER.readTree(response.body());
    }

    private String admin() {
        return token("ADMIN");
    }

    private String token(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(adminId)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }
}
