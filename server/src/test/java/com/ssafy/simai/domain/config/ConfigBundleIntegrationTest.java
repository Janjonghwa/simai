package com.ssafy.simai.domain.config;

import static org.assertj.core.api.Assertions.assertThat;

import com.networknt.schema.InputFormat;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SpecVersion;
import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import org.assertj.core.data.Offset;
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
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 구역·작업면·보정 문서·구성 묶음(S15P31A101-120·-58, 명세 3.2·7.5·8.2·12.6). 실제 포트로 관리자 API 를 부르고 Tier 0 처럼 I1 에 붙어
 * config_bundle 을 받고, 서명 주소로 파일을 내려받아 해시를 맞춰 본 뒤 config_ack 를 보낸다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class ConfigBundleIntegrationTest {

    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final JsonSchemaFactory SCHEMAS = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012,
            builder -> builder.schemaMappers(m -> m.mapPrefix("https://simai.dev/contracts/",
                    CONTRACTS.toAbsolutePath().toUri().toString())));
    // 시험 설정의 public-base-url. Tier 0 는 이 주소로 받는다. 시험은 같은 경로를 로컬 포트로 부른다
    private static final String PUBLIC_BASE = "https://ec2.test";

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String gatewayId = "tier0-config-" + tag;
    private final List<String> bundles = new ArrayList<>();
    private long adminId;

    @BeforeEach
    void users() {
        adminId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id", Long.class, "config-admin-" + tag);
    }

    @AfterEach
    void cleanUp() {
        for (String version : bundles.reversed()) {
            jdbc.update("DELETE FROM config_ack WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle_file WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", version);
        }
        jdbc.update("DELETE FROM system_health WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM zone WHERE created_by = ?", adminId);
        jdbc.update("DELETE FROM static_object WHERE created_by = ?", adminId);
        jdbc.update("DELETE FROM calibration_document WHERE uploaded_by = ?", adminId);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version LIKE ?", "cal-test-" + tag + "%");
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 구역은_수정하면_새_버전을_더하고_이전_버전을_닫는다() throws Exception {
        JsonNode created = call("POST", "/api/zones", admin(), zone("개구부 위", 3.0), 201);
        String zoneId = created.get("zone_id").asString();
        assertThat(created.get("zone_version").intValue()).isEqualTo(1);
        assertThat(created.get("polygon_xy")).hasSize(4);
        assertThat(created.get("valid_to").isNull()).isTrue();

        JsonNode updated = call("PUT", "/api/zones/" + zoneId, admin(), zone("개구부 위(넓힘)", 4.0), 200);
        assertThat(updated.get("zone_version").intValue()).isEqualTo(2);

        JsonNode current = call("GET", "/api/zones", viewer(), null, 200);
        List<JsonNode> mine = current.valueStream().filter(z -> z.get("zone_id").asString().equals(zoneId)).toList();
        assertThat(mine).hasSize(1);
        assertThat(mine.getFirst().get("zone_version").intValue()).isEqualTo(2);
        assertThat(mine.getFirst().get("name").asString()).isEqualTo("개구부 위(넓힘)");

        JsonNode versions = call("GET", "/api/zones/" + zoneId + "/versions", viewer(), null, 200);
        assertThat(versions).hasSize(2);
        assertThat(versions.get(0).get("valid_to").longValue()).isEqualTo(versions.get(1).get("valid_from").longValue());
    }

    @Test
    void 꼬인_다각형과_높이_없는_VOLUME_3D_와_관리자_아닌_쓰기는_거부한다() throws Exception {
        ObjectNode bowtie = zone("나비", 3.0);
        bowtie.putArray("polygon_xy").add(point(0, 0)).add(point(2, 2)).add(point(2, 0)).add(point(0, 2));
        assertThat(call("POST", "/api/zones", admin(), bowtie, 400).get("code").asString()).isEqualTo("INVALID_POLYGON");

        ObjectNode noHeight = zone("높이 없음", 3.0);
        noHeight.remove("z_max_m");
        assertThat(call("POST", "/api/zones", admin(), noHeight, 400).get("code").asString()).isEqualTo("INVALID_Z_RANGE");

        call("POST", "/api/zones", viewer(), zone("열람", 3.0), 403);
        call("PUT", "/api/zones/z-none", admin(), zone("없음", 3.0), 404);
    }

    @Test
    void 작업면은_상판_높이가_있어야_하고_새_버전을_더한다() throws Exception {
        JsonNode surface = call("POST", "/api/static-objects", admin(), podium(0.4), 201);
        assertThat(surface.get("object_id").asString()).startsWith("ws-");
        assertThat(surface.get("top_height_m").doubleValue()).isEqualTo(0.4, Offset.offset(1e-6));
        assertThat(surface.at("/obb/yaw_rad").doubleValue()).isEqualTo(0.0);

        JsonNode next = call("PUT", "/api/static-objects/" + surface.get("object_id").asString(), admin(), podium(0.45), 200);
        assertThat(next.get("object_version").intValue()).isEqualTo(2);

        ObjectNode noTop = podium(0.4);
        noTop.remove("top_height_m");
        assertThat(call("POST", "/api/static-objects", admin(), noTop, 400).get("code").asString())
                .isEqualTo("INVALID_STATIC_OBJECT");
    }

    @Test
    void 보정_문서는_계약에_맞아야_하고_같은_버전은_같은_문서만_받는다() throws Exception {
        ObjectNode doc = calibration("cal-test-" + tag + "-a");
        JsonNode uploaded = call("POST", "/api/calibrations", admin(), upload(doc), 201);
        assertThat(uploaded.get("calibration_version").asString()).isEqualTo("cal-test-" + tag + "-a");
        assertThat(uploaded.get("sha256").asString()).startsWith("sha256:");
        // 같은 문서를 다시 올리면 그대로 받는다
        call("POST", "/api/calibrations", admin(), upload(doc), 201);

        ObjectNode changed = doc.deepCopy();
        changed.put("state", "CALIBRATED");
        assertThat(call("POST", "/api/calibrations", admin(), upload(changed), 409).get("code").asString())
                .isEqualTo("CALIBRATION_VERSION_CONFLICT");

        ObjectNode broken = calibration("cal-test-" + tag + "-b");
        broken.remove("cameras");
        assertThat(call("POST", "/api/calibrations", admin(), upload(broken), 400).get("code").asString())
                .isEqualTo("INVALID_CALIBRATION");
    }

    @Test
    void 구성_묶음을_발행하면_Tier0_가_서명_주소로_받아_검증하고_APPLIED_로_적용된다() throws Exception {
        JsonNode zone = call("POST", "/api/zones", admin(), zone("양중 하부", 5.0), 201);
        JsonNode surface = call("POST", "/api/static-objects", admin(), podium(0.4), 201);
        ObjectNode calibrationDoc = calibration("cal-test-" + tag + "-c");
        call("POST", "/api/calibrations", admin(), upload(calibrationDoc), 201);

        try (Tier0 tier0 = tier0()) {
            // 연결을 gateway 에 묶는다(첫 메시지의 source_id)
            tier0.send(health());
            Thread.sleep(300);
            JsonNode published = publish("CLASSROOM_TEST");
            String version = published.get("config_version").asString();
            assertThat(published.get("status").asString()).isEqualTo("PENDING");
            assertThat(published.get("rule_profile").asString()).isEqualTo("CLASSROOM_TEST");
            assertThat(published.get("calibration_version").asString()).isEqualTo("cal-test-" + tag + "-c");
            assertThat(published.toString()).doesNotContain("sig=");

            JsonNode notice = tier0.await("config_bundle", version);
            assertThat(schemaViolations("config_bundle", notice)).isEmpty();
            JsonNode p = notice.get("payload");
            assertThat(p.get("bundle_uri").asString()).startsWith(PUBLIC_BASE + "/api/config-bundles/" + version + "/files/manifest?sig=");

            // Tier 0 검증 순서(ai/src/simai_ai/config/verify.py): 매니페스트 해시 → 매니페스트 = 알림 → 구성 요소 해시·버전
            byte[] manifestBytes = download(p.get("bundle_uri").asString());
            assertThat(ConfigFiles.sha256(manifestBytes)).isEqualTo(p.get("bundle_hash").asString());
            JsonNode manifest = MAPPER.readTree(manifestBytes);
            assertThat(manifest.get("config_version").asString()).isEqualTo(version);
            assertThat(manifest.get("components")).isEqualTo(p.get("components"));
            assertThat(manifest.get("issued_at").longValue()).isEqualTo(p.get("issued_at").longValue());

            for (String name : ConfigFiles.COMPONENTS) {
                JsonNode ref = p.at("/components/" + name);
                byte[] bytes = download(ref.get("uri").asString());
                assertThat(ConfigFiles.sha256(bytes)).as(name).isEqualTo(ref.get("sha256").asString());
                JsonNode doc = MAPPER.readTree(bytes);
                String key = name.equals("calibration") ? "calibration_version" : "version";
                assertThat(doc.get(key).asString()).as(name).isEqualTo(ref.get("version").asString());
            }
            JsonNode zones = MAPPER.readTree(download(p.at("/components/zones/uri").asString()));
            JsonNode mine = zones.get("zones").valueStream()
                    .filter(z -> z.get("zone_id").asString().equals(zone.get("zone_id").asString())).findFirst().orElseThrow();
            // Tier 0 Zone.from_json 이 받는 키만 있다
            assertThat(mine.propertyNames()).isSubsetOf("zone_id", "zone_version", "judge_type", "polygon_xy", "z_range", "severity",
                    "zone_type", "valid_from", "valid_to");
            assertThat(mine.get("zone_version").asString()).isEqualTo("1");
            assertThat(mine.get("judge_type").asString()).isEqualTo("VOLUME_3D");
            assertThat(mine.get("z_range")).hasSize(2);

            JsonNode geometry = MAPPER.readTree(download(p.at("/components/operational_geometry/uri").asString()));
            JsonNode podium = geometry.get("work_surfaces").valueStream()
                    .filter(s -> s.get("surface_id").asString().equals(surface.get("object_id").asString())).findFirst().orElseThrow();
            assertThat(podium.get("top_height_m").doubleValue()).isEqualTo(0.4, Offset.offset(1e-6));
            assertThat(podium.get("center_xy")).hasSize(2);
            assertThat(podium.get("size_xy")).hasSize(2);

            JsonNode rules = MAPPER.readTree(download(p.at("/components/rules/uri").asString()));
            assertThat(rules.get("rule_versions")).isEqualTo(p.at("/components/rules/rule_versions"));
            assertThat(rules.get("rule_versions").valueStream().map(r -> r.get("rule_id").asString() + ":" + r.get("rule_version").intValue()))
                    .contains("R2:1001", "R3:1001", "R1:1").doesNotContain("R2:1");
            assertThat(download(p.at("/components/calibration/uri").asString()))
                    .isEqualTo(MAPPER.writeValueAsBytes(calibrationDoc));

            // 서명이 틀리면 없는 것과 같다
            String forged = p.get("bundle_uri").asString().replaceAll("sig=.*$", "sig=forged");
            assertThat(get(local(forged)).statusCode()).isEqualTo(404);

            ObjectNode ack = ack(version, p.get("bundle_hash").asString());
            tier0.send(ack);
            tier0.send(ack); // 같은 msg_id 는 한 번만 남는다
            JsonNode applied = awaitStatus(version, "APPLIED");
            assertThat(applied.get("applied_at").longValue()).isEqualTo(ack.at("/payload/applied_at").longValue());
            assertThat(applied.at("/last_ack/gateway_id").asString()).isEqualTo(gatewayId);
            assertThat(jdbc.queryForObject("SELECT count(*) FROM config_ack WHERE config_version = ?", Integer.class, version))
                    .isEqualTo(1);
            assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle_item WHERE config_version = ? AND item_id IN (?, ?)",
                    Integer.class, version, zone.get("zone_id").asString(), surface.get("object_id").asString())).isEqualTo(2);
        }
    }

    @Test
    void 다음_묶음은_이전_묶음을_가리키고_규칙_프로필을_이어받는다() throws Exception {
        call("POST", "/api/calibrations", admin(), upload(calibration("cal-test-" + tag + "-d")), 201);
        JsonNode first = publish("CLASSROOM_TEST");
        JsonNode second = publish(null);
        assertThat(second.get("previous_config_version").asString()).isEqualTo(first.get("config_version").asString());
        assertThat(second.get("rule_profile").asString()).isEqualTo("CLASSROOM_TEST");
        assertThat(call("POST", "/api/config-bundles", admin(), MAPPER.createObjectNode().put("rule_profile", "FAST"), 400)
                .get("code").asString()).isEqualTo("INVALID_RULE_PROFILE");
        call("POST", "/api/config-bundles", viewer(), MAPPER.createObjectNode(), 403);
    }

    // ---- 도우미

    private JsonNode publish(String profile) throws Exception {
        ObjectNode body = MAPPER.createObjectNode();
        if (profile != null) {
            body.put("rule_profile", profile);
        }
        JsonNode published = call("POST", "/api/config-bundles", admin(), body, 201);
        bundles.add(published.get("config_version").asString());
        return published;
    }

    private JsonNode awaitStatus(String version, String status) throws Exception {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        JsonNode view = call("GET", "/api/config-bundles/" + version, viewer(), null, 200);
        while (!status.equals(view.get("status").asString()) && System.nanoTime() < deadline) {
            Thread.sleep(100);
            view = call("GET", "/api/config-bundles/" + version, viewer(), null, 200);
        }
        assertThat(view.get("status").asString()).isEqualTo(status);
        return view;
    }

    private List<String> schemaViolations(String msgType, JsonNode message) {
        return SCHEMAS.getSchema(SchemaLocation.of("https://simai.dev/contracts/messages/" + msgType + ".schema.json"))
                .validate(MAPPER.writeValueAsString(message), InputFormat.JSON).stream().map(Object::toString).toList();
    }

    private ObjectNode zone(String name, double zMax) {
        ObjectNode z = MAPPER.createObjectNode();
        z.put("name", name);
        z.put("zone_kind", "OPENING");
        z.put("eval_type", "VOLUME_3D");
        z.putArray("polygon_xy").add(point(0, 0)).add(point(4, 0)).add(point(4, 3)).add(point(0, 3));
        z.put("z_min_m", 0.0);
        z.put("z_max_m", zMax);
        z.put("severity", "CRITICAL");
        return z;
    }

    private ArrayNode point(double x, double y) {
        return MAPPER.createArrayNode().add(x).add(y);
    }

    private ObjectNode podium(double top) {
        ObjectNode o = MAPPER.createObjectNode();
        o.put("kind", "WORK_SURFACE");
        o.put("name", "교탁 단상");
        ObjectNode obb = o.putObject("obb");
        obb.putArray("center").add(4.5).add(6.75).add(top / 2);
        obb.putArray("size").add(4.0).add(1.1).add(top);
        o.put("top_height_m", top);
        return o;
    }

    private ObjectNode calibration(String version) throws Exception {
        ObjectNode doc = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples/calibration.json")));
        doc.put("calibration_version", version);
        return doc;
    }

    private ObjectNode upload(ObjectNode doc) {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("reason", "시험 보정");
        body.set("document", doc);
        return body;
    }

    private ObjectNode health() throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples/health.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        return root;
    }

    private ObjectNode ack(String version, String hash) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples/config_ack.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("config_version", version);
        p.put("bundle_hash", hash);
        p.put("active_config_version", version);
        p.put("applied_at", Instant.now().toEpochMilli());
        return root;
    }

    private String admin() {
        return token("ADMIN");
    }

    private String viewer() {
        return token("VIEWER");
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

    private String local(String publicUrl) {
        assertThat(publicUrl).startsWith(PUBLIC_BASE);
        return "http://localhost:" + port + publicUrl.substring(PUBLIC_BASE.length());
    }

    /** Tier 0 처럼 헤더 없이 받는다 */
    private byte[] download(String publicUrl) throws Exception {
        HttpResponse<byte[]> response = get(local(publicUrl));
        assertThat(response.statusCode()).as(publicUrl).isEqualTo(200);
        return response.body();
    }

    private HttpResponse<byte[]> get(String url) throws Exception {
        return http.send(HttpRequest.newBuilder(URI.create(url)).GET().build(), HttpResponse.BodyHandlers.ofByteArray());
    }

    private Tier0 tier0() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TIER0_TOKEN);
        BlockingQueue<JsonNode> received = new LinkedBlockingQueue<>();
        WebSocketSession session = new StandardWebSocketClient().execute(new TextWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                received.add(MAPPER.readTree(message.getPayload()));
            }
        }, headers, URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS);
        return new Tier0(session, received);
    }

    /** Tier 0 흉내. EC2 가 보낸 메시지를 모은다 */
    private record Tier0(WebSocketSession session, BlockingQueue<JsonNode> received) implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(MAPPER.writeValueAsString(message)));
        }

        JsonNode await(String msgType, String configVersion) throws InterruptedException {
            long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
            while (System.nanoTime() < deadline) {
                JsonNode message = received.poll(200, TimeUnit.MILLISECONDS);
                if (message != null && msgType.equals(message.path("msg_type").asString())
                        && configVersion.equals(message.at("/payload/config_version").asString())) {
                    return message;
                }
            }
            throw new AssertionError(msgType + " " + configVersion + " 가 오지 않았다");
        }

        @Override
        public void close() throws Exception {
            session.close();
        }
    }
}
