package com.ssafy.simai.domain.zone;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
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
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** 구역 편집 한 번 저장(POST /api/zones/commit)과 작업면 zone_id 연결 통합 시험(S15P31A101-397) */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class ZoneCommitIntegrationTest {

    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final List<String> bundles = new ArrayList<>();
    private long adminId;

    @BeforeEach
    void setUp() throws Exception {
        adminId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id", Long.class, "commit-admin-" + tag);
        // 묶음에는 보정 문서가 있어야 한다
        ObjectNode doc = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples/calibration.json")));
        doc.put("calibration_version", "cal-commit-" + tag);
        ObjectNode upload = MAPPER.createObjectNode();
        upload.put("reason", "시험 보정");
        upload.set("document", doc);
        call("POST", "/api/calibrations", token("ADMIN"), upload, 201);
    }

    @AfterEach
    void cleanUp() {
        for (String version : bundles.reversed()) {
            jdbc.update("DELETE FROM config_ack WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle_file WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", version);
        }
        jdbc.update("DELETE FROM zone WHERE created_by = ?", adminId);
        jdbc.update("DELETE FROM static_object WHERE created_by = ?", adminId);
        jdbc.update("DELETE FROM calibration_document WHERE uploaded_by = ?", adminId);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", "cal-commit-" + tag);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 새_구역과_새_작업면을_한_번에_만들면_작업면이_그_구역에_이어지고_묶음에_둘_다_실린다() throws Exception {
        ObjectNode body = commit("CREATE", null, zone("단상 구역"), "CREATE", null, podium(0.4), null);
        JsonNode result = call("POST", "/api/zones/commit", token("ADMIN"), body, 200);
        String zoneId = result.get("zone_id").asString();
        String surfaceId = result.at("/surface/object_id").asString();
        String version = track(result);
        assertThat(result.at("/zone/zone_version").asInt()).isEqualTo(1);
        assertThat(result.at("/surface/zone_id").asString()).isEqualTo(zoneId);
        assertThat(version).startsWith("cfg-");
        List<String> items = result.at("/bundle/items").valueStream().map(i -> i.get("item_id").asString()).toList();
        assertThat(items).contains(zoneId, surfaceId);

        JsonNode listed = call("GET", "/api/static-objects", token("VIEWER"), null, 200);
        JsonNode mine = listed.valueStream().filter(o -> o.get("object_id").asString().equals(surfaceId)).findFirst().orElseThrow();
        assertThat(mine.get("zone_id").asString()).isEqualTo(zoneId);
    }

    @Test
    void 한_단계라도_실패하면_구역_작업면_묶음이_모두_그대로다() throws Exception {
        JsonNode created = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("CREATE", null, zone("단상 구역"), "CREATE", null, podium(0.4), false), 200);
        String zoneId = created.get("zone_id").asString();
        assertThat(created.get("bundle").isNull()).isTrue();
        int bundlesBefore = jdbc.queryForObject("SELECT count(*) FROM config_bundle", Integer.class);

        // 구역은 고쳐지지만 작업면 값이 틀리면(상판 0) 구역 새 버전도 남지 않는다
        JsonNode bad = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("UPDATE", zoneId, zone("단상 구역(넓힘)"), "CREATE", null, podium(0.0), null), 400);
        assertThat(bad.get("code").asString()).isEqualTo("INVALID_STATIC_OBJECT");
        // 이미 이어진 작업면이 있는데 새로 만들면 409, 역시 아무것도 바뀌지 않는다
        JsonNode taken = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("UPDATE", zoneId, zone("단상 구역(넓힘)"), "CREATE", null, podium(0.5), null), 409);
        assertThat(taken.get("code").asString()).isEqualTo("SURFACE_ZONE_TAKEN");

        assertThat(jdbc.queryForObject("SELECT max(zone_version) FROM zone WHERE zone_id = ?", Integer.class, zoneId)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM static_object WHERE created_by = ?", Integer.class, adminId)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle", Integer.class)).isEqualTo(bundlesBefore);
    }

    @Test
    void 작업면만_바꾸거나_바꿔_끼우고_구역을_보관하면_이은_작업면도_보관한다() throws Exception {
        JsonNode created = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("CREATE", null, zone("단상 구역"), "CREATE", null, podium(0.4), false), 200);
        String zoneId = created.get("zone_id").asString();
        String first = created.at("/surface/object_id").asString();

        // KEEP + UPDATE: 구역은 그대로, 작업면 새 버전은 이 구역에 이어진다
        JsonNode updated = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("KEEP", zoneId, null, "UPDATE", first, podium(0.45), false), 200);
        assertThat(updated.get("zone").isNull()).isTrue();
        assertThat(updated.at("/surface/object_version").asInt()).isEqualTo(2);
        assertThat(updated.at("/surface/zone_id").asString()).isEqualTo(zoneId);

        // 예전 API 로 zone_id 없이 고쳐도 연결은 남고, 빈 글자면 풀린다
        JsonNode viaPut = call("PUT", "/api/static-objects/" + first, token("ADMIN"), podium(0.5), 200);
        assertThat(viaPut.get("zone_id").asString()).isEqualTo(zoneId);
        ObjectNode unlink = podium(0.5);
        unlink.put("zone_id", "");
        assertThat(call("PUT", "/api/static-objects/" + first, token("ADMIN"), unlink, 200).get("zone_id").isNull()).isTrue();
        ObjectNode relink = podium(0.5);
        relink.put("zone_id", zoneId);
        assertThat(call("PUT", "/api/static-objects/" + first, token("ADMIN"), relink, 200).get("zone_id").asString())
                .isEqualTo(zoneId);

        // REPLACE: 옛 작업면을 보관하고 새 작업면을 잇는다
        JsonNode replaced = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("KEEP", zoneId, null, "REPLACE", first, podium(0.6), false), 200);
        String second = replaced.at("/surface/object_id").asString();
        assertThat(second).isNotEqualTo(first);
        assertThat(replaced.get("archived_surface_id").asString()).isEqualTo(first);
        assertThat(replaced.at("/surface/zone_id").asString()).isEqualTo(zoneId);
        assertThat(current("static_object", "object_id", first)).isZero();

        // 구역 보관: 이은 작업면도 보관하고 묶음을 만든다
        JsonNode archived = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("ARCHIVE", zoneId, null, null, null, null, null), 200);
        track(archived);
        assertThat(archived.get("archived_surface_id").asString()).isEqualTo(second);
        assertThat(current("zone", "zone_id", zoneId)).isZero();
        assertThat(current("static_object", "object_id", second)).isZero();
        List<String> items = archived.at("/bundle/items").valueStream().map(i -> i.get("item_id").asString()).toList();
        assertThat(items).doesNotContain(zoneId, second);
    }

    @Test
    void 예전_API_로_구역을_보관해도_작업면_수정이_막히지_않고_다른_구역으로_옮길_수_있다() throws Exception {
        JsonNode a = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("CREATE", null, zone("가 구역"), "CREATE", null, podium(0.4), false), 200);
        JsonNode b = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("CREATE", null, zone("나 구역"), null, null, null, false), 200);
        String zoneA = a.get("zone_id").asString();
        String surface = a.at("/surface/object_id").asString();
        String zoneB = b.get("zone_id").asString();

        // DELETE /api/zones 는 작업면을 남긴다. zone_id 를 모르는 화면의 수정은 보관된 구역 연결을 조용히 푼다
        call("DELETE", "/api/zones/" + zoneA, token("ADMIN"), null, 204);
        JsonNode edited = call("PUT", "/api/static-objects/" + surface, token("ADMIN"), podium(0.5), 200);
        assertThat(edited.get("zone_id").isNull()).isTrue();

        // 연결이 풀린 작업면은 다른 구역으로 가져올 수 있다
        JsonNode moved = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("KEEP", zoneB, null, "UPDATE", surface, podium(0.5), false), 200);
        assertThat(moved.at("/surface/zone_id").asString()).isEqualTo(zoneB);

        // 장애물로 바꾸면 이어받던 연결을 푼다(직접 zone_id 를 주지 않으면 400 이 아니다)
        ObjectNode obstacle = podium(0.5);
        obstacle.put("kind", "OBSTACLE");
        assertThat(call("PUT", "/api/static-objects/" + surface, token("ADMIN"), obstacle, 200).get("zone_id").isNull()).isTrue();
    }

    @Test
    void 권한과_요청_모양_반례를_거부한다() throws Exception {
        JsonNode a = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("CREATE", null, zone("가 구역"), "CREATE", null, podium(0.4), false), 200);
        JsonNode b = call("POST", "/api/zones/commit", token("ADMIN"),
                commit("CREATE", null, zone("나 구역"), null, null, null, false), 200);
        String zoneA = a.get("zone_id").asString();
        String surfaceA = a.at("/surface/object_id").asString();
        String zoneB = b.get("zone_id").asString();

        call("POST", "/api/zones/commit", token("FOREMAN"), commit("KEEP", zoneA, null, "ARCHIVE", surfaceA, null, false), 403);

        ObjectNode[] invalid = {
                MAPPER.createObjectNode(),
                commit("DELETE", zoneA, null, null, null, null, false),
                commit("CREATE", zoneA, zone("x"), null, null, null, false),
                commit("UPDATE", zoneA, null, null, null, null, false),
                commit("ARCHIVE", zoneA, null, "ARCHIVE", surfaceA, null, false),
                commit("KEEP", zoneA, null, null, null, null, false),
                commit("KEEP", zoneA, null, "UPDATE", null, podium(0.4), false),
                commit("KEEP", zoneA, null, "CREATE", surfaceA, podium(0.4), false),
                commit("KEEP", zoneA, null, "ARCHIVE", surfaceA, podium(0.4), false),
                commit("KEEP", zoneA, null, "MOVE", surfaceA, null, false)
        };
        for (ObjectNode body : invalid) {
            JsonNode error = call("POST", "/api/zones/commit", token("ADMIN"), body, 400);
            assertThat(error.get("code").asString()).as(body.toString()).isEqualTo("INVALID_ZONE_COMMIT");
        }
        // 다른 구역에 이어진 작업면은 가져오지 않는다
        assertThat(call("POST", "/api/zones/commit", token("ADMIN"),
                commit("KEEP", zoneB, null, "UPDATE", surfaceA, podium(0.4), false), 409).get("code").asString())
                .isEqualTo("SURFACE_ZONE_TAKEN");
        // 없는 구역
        assertThat(call("POST", "/api/zones/commit", token("ADMIN"),
                commit("KEEP", "z-none-" + tag, null, "CREATE", null, podium(0.4), false), 404).get("code").asString())
                .isEqualTo("ZONE_NOT_FOUND");
        // 장애물은 잇지 않고, 없는 구역에는 잇지 않으며, 한 구역에 작업면은 하나다
        ObjectNode obstacle = podium(0.4);
        obstacle.put("kind", "OBSTACLE");
        obstacle.put("zone_id", zoneB);
        assertThat(call("POST", "/api/static-objects", token("ADMIN"), obstacle, 400).get("code").asString())
                .isEqualTo("INVALID_SURFACE_ZONE");
        ObjectNode nowhere = podium(0.4);
        nowhere.put("zone_id", "z-none-" + tag);
        assertThat(call("POST", "/api/static-objects", token("ADMIN"), nowhere, 400).get("code").asString())
                .isEqualTo("INVALID_SURFACE_ZONE");
        ObjectNode second = podium(0.4);
        second.put("zone_id", zoneA);
        assertThat(call("POST", "/api/static-objects", token("ADMIN"), second, 409).get("code").asString())
                .isEqualTo("SURFACE_ZONE_TAKEN");
    }

    // ---- 도우미

    private int current(String table, String column, String id) {
        return jdbc.queryForObject("SELECT count(*) FROM " + table + " WHERE " + column + " = ? AND valid_to IS NULL",
                Integer.class, id);
    }

    private String track(JsonNode result) {
        String version = result.at("/bundle/config_version").asString();
        bundles.add(version);
        return version;
    }

    private static ObjectNode commit(String zoneOp, String zoneId, ObjectNode zoneBody, String surfaceOp, String objectId,
            ObjectNode surfaceBody, Boolean publish) {
        ObjectNode body = MAPPER.createObjectNode();
        ObjectNode zone = body.putObject("zone");
        zone.put("op", zoneOp);
        if (zoneId != null) {
            zone.put("zone_id", zoneId);
        }
        if (zoneBody != null) {
            zone.set("body", zoneBody);
        }
        if (surfaceOp != null) {
            ObjectNode surface = body.putObject("surface");
            surface.put("op", surfaceOp);
            if (objectId != null) {
                surface.put("object_id", objectId);
            }
            if (surfaceBody != null) {
                surface.set("body", surfaceBody);
            }
        }
        if (publish != null) {
            body.put("publish", publish);
        }
        return body;
    }

    private static ObjectNode zone(String name) {
        ObjectNode z = MAPPER.createObjectNode();
        z.put("name", name);
        z.put("zone_kind", "PLATFORM");
        z.put("eval_type", "GROUND_FOOTPRINT");
        z.putArray("polygon_xy").add(point(0, 0)).add(point(4, 0)).add(point(4, 3)).add(point(0, 3));
        z.put("severity", "HIGH");
        return z;
    }

    private static ArrayNode point(double x, double y) {
        return MAPPER.createArrayNode().add(x).add(y);
    }

    private static ObjectNode podium(double top) {
        ObjectNode o = MAPPER.createObjectNode();
        o.put("kind", "WORK_SURFACE");
        o.put("name", "단상");
        ObjectNode obb = o.putObject("obb");
        obb.putArray("center").add(2.0).add(1.5).add(Math.max(top, 0.1) / 2);
        obb.putArray("size").add(2.0).add(1.0).add(Math.max(top, 0.1));
        o.put("top_height_m", top);
        return o;
    }

    private String token(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(adminId)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
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
}
