package com.ssafy.simai.domain.config;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.doReturn;

import com.networknt.schema.InputFormat;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SpecVersion;
import com.ssafy.simai.domain.calibration.CalibrationViews;
import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
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
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
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
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
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

    /** 기본은 실제 동작. 묶음 만들기 실패 시험만 지금 보정 버전을 바꾼다(시험마다 초기화) */
    @MockitoSpyBean
    CalibrationViews calibrations;

    @Autowired
    I1ConnectionRegistry i1Connections;

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
        // 작업 시작·종료·구역 저장이 만든 묶음도 지운다(발행자 = 이 시험 관리자)
        for (String version : jdbc.queryForList("SELECT config_version FROM config_bundle WHERE issued_by = ?", String.class, adminId)) {
            if (!bundles.contains(version)) {
                bundles.add(version);
            }
        }
        bundles.sort(java.util.Comparator.comparingInt(v -> Integer.parseInt(v.substring(4))));
        for (String version : bundles.reversed()) {
            jdbc.update("DELETE FROM config_ack WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle_file WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", version);
        }
        jdbc.update("DELETE FROM report WHERE work_id IN (SELECT work_id FROM work_unit WHERE name = ?)", workName());
        jdbc.update("DELETE FROM work_unit WHERE name = ?", workName());
        jdbc.update("DELETE FROM system_health WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM zone WHERE created_by = ?", adminId);
        jdbc.update("DELETE FROM static_object WHERE created_by = ?", adminId);
        jdbc.update("DELETE FROM calibration_document WHERE uploaded_by = ?", adminId);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version LIKE ?", "cal-test-" + tag + "%");
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
        // 프로필을 지정한 발행·기준 바꾸기는 현장 설정을 바꾼다(S15P31A101-654). 다른 시험에 남기지 않고 기본(교실 시험, -669)으로 되돌린다
        jdbc.update("UPDATE site SET rule_profile = 'CLASSROOM_TEST'");
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
            JsonNode ppe = MAPPER.readTree(download(p.at("/components/ppe_policy/uri").asString()));
            assertThat(dataViolations("ppe_policy", ppe)).isEmpty();
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
                    .contains("R2:1001", "R3:1001", "R1:1", "R9:1", "R11:1").doesNotContain("R2:1");
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
            // Lead 시연 예외(-556): 시드 → 발행 묶음 → APPLIED → 규칙 조회까지 PPE 두 규칙이 이어진다.
            JsonNode activeRules = call("GET", "/api/rules", admin(), null, 200).get("rules");
            for (String id : List.of("R9", "R11")) {
                JsonNode rule = activeRules.valueStream().filter(r -> r.get("rule_id").asString().equals(id))
                        .findFirst().orElseThrow();
                assertThat(rule.get("active").asBoolean()).isTrue();
                assertThat(rule.at("/current/rule_version").asInt()).isEqualTo(1);
            }
        }
    }

    /** S15P31A101-518: 작업 시작·종료가 ppe_policy 를 실은 새 묶음을 발행하고, 다른 경로의 묶음도 그때 작업 상태를 싣는다 */
    @Test
    void 작업_시작_종료는_안전모_조끼_요구를_실은_새_묶음을_내고_다른_경로의_묶음도_요구를_싣는다() throws Exception {
        assertThat(jdbc.queryForObject("SELECT count(*) FROM work_unit WHERE status = 'IN_PROGRESS'", Integer.class))
                .as("다른 시험이 남긴 진행 중 작업").isZero();
        long work = createWork();
        call("POST", "/api/calibrations", admin(), upload(calibration("cal-test-" + tag + "-p")), 201);

        try (Tier0 tier0 = tier0()) {
            tier0.send(health());
            Thread.sleep(300);
            // 작업 전 발행: 요구 없음
            JsonNode before = publish(null);
            assertThat(before.at("/ppe_policy/helmet").asString()).isEqualTo("NOT_REQUIRED");
            assertThat(before.at("/ppe_policy/vest").asString()).as("조끼 요구(-549)").isEqualTo("NOT_REQUIRED");
            assertThat(before.at("/ppe_policy/work_id").isNull()).isTrue();

            JsonNode started = call("POST", "/api/work-units/" + work + "/start", admin(), null, 200);
            long startedAt = started.get("actual_started_at").longValue();
            String startVersion = latestVersion();
            assertThat(startVersion).isNotEqualTo(before.get("config_version").asString());
            JsonNode startNotice = tier0.await("config_bundle", startVersion);
            assertThat(schemaViolations("config_bundle", startNotice)).isEmpty();
            assertThat(startNotice.get("schema_version").asString()).isEqualTo("1.4.0");
            assertThat(startNotice.at("/payload/previous_config_version").asString()).isEqualTo(before.get("config_version").asString());
            JsonNode startPolicy = ppeDocument(startNotice);
            assertThat(startPolicy.get("helmet").asString()).isEqualTo("REQUIRED");
            assertThat(startPolicy.get("vest").asString()).isEqualTo("REQUIRED");
            assertThat(startPolicy.get("format_version").asString()).isEqualTo("1.2.0");
            assertThat(startPolicy.get("work_id").longValue()).isEqualTo(work);
            assertThat(startPolicy.get("effective_from_ms").longValue()).isEqualTo(startedAt);
            JsonNode startView = call("GET", "/api/config-bundles/" + startVersion, viewer(), null, 200);
            assertThat(startView.get("status").asString()).isEqualTo("PENDING");
            assertThat(startView.at("/ppe_policy/helmet").asString()).isEqualTo("REQUIRED");
            assertThat(startView.at("/ppe_policy/vest").asString()).isEqualTo("REQUIRED");
            assertThat(startView.at("/components/ppe_policy/sha256").asString())
                    .isEqualTo(startNotice.at("/payload/components/ppe_policy/sha256").asString());

            // 작업 중 다른 경로(직접 발행, 구역 저장)도 요구를 싣는다
            JsonNode during = publish(null);
            assertThat(during.at("/ppe_policy/helmet").asString()).isEqualTo("REQUIRED");
            assertThat(during.at("/ppe_policy/work_id").longValue()).isEqualTo(work);
            ObjectNode commit = MAPPER.createObjectNode();
            commit.putObject("zone").put("op", "CREATE").set("body", zone("작업 중 저장", 3.0));
            JsonNode committed = call("POST", "/api/zones/commit", admin(), commit, 200);
            String commitVersion = committed.at("/bundle/config_version").asString();
            assertThat(committed.at("/bundle/ppe_policy/helmet").asString()).isEqualTo("REQUIRED");
            JsonNode commitPolicy = ppeDocument(tier0.await("config_bundle", commitVersion));
            assertThat(commitPolicy.get("helmet").asString()).isEqualTo("REQUIRED");
            assertThat(commitPolicy.get("effective_from_ms").longValue()).isEqualTo(startedAt);

            JsonNode ended = call("POST", "/api/work-units/" + work + "/end", admin(), null, 202);
            long endedAt = ended.at("/work_unit/actual_ended_at").longValue();
            String endVersion = latestVersion();
            assertThat(endVersion).isNotEqualTo(commitVersion);
            JsonNode endNotice = tier0.await("config_bundle", endVersion);
            assertThat(schemaViolations("config_bundle", endNotice)).isEmpty();
            JsonNode endPolicy = ppeDocument(endNotice);
            assertThat(endPolicy.get("helmet").asString()).isEqualTo("NOT_REQUIRED");
            assertThat(endPolicy.get("vest").asString()).isEqualTo("NOT_REQUIRED");
            assertThat(endPolicy.get("work_id").isNull()).isTrue();
            assertThat(endPolicy.get("effective_from_ms").longValue()).isEqualTo(endedAt);

            // 작업이 끝난 뒤 발행: 요구 없음, 유효 시각은 마지막 종료 시각
            JsonNode after = publish(null);
            assertThat(after.at("/ppe_policy/helmet").asString()).isEqualTo("NOT_REQUIRED");
            assertThat(after.at("/ppe_policy/effective_from_ms").longValue()).isEqualTo(endedAt);
        }
    }

    /** S15P31A101-654: 작업 시작 때 끈 장비는 시작 묶음의 ppe_policy 1.2.0 에 NOT_REQUIRED 로 실리고 작업에도 남는다 */
    @Test
    void 작업_시작에서_끈_보호구는_시작_묶음에_NOT_REQUIRED_로_실린다() throws Exception {
        long work = createWork();
        assertThat(call("GET", "/api/work-units/" + work, viewer(), null, 200).get("helmet").asString())
                .as("등록 기본값은 필수").isEqualTo("REQUIRED");
        call("POST", "/api/calibrations", admin(), upload(calibration("cal-test-" + tag + "-q")), 201);
        assertThat(call("POST", "/api/work-units/" + work + "/start", admin(),
                MAPPER.createObjectNode().put("helmet", "MAYBE"), 400).get("code").asString()).isEqualTo("INVALID_WORK_UNIT");

        try (Tier0 tier0 = tier0()) {
            tier0.send(health());
            Thread.sleep(300);
            JsonNode started = call("POST", "/api/work-units/" + work + "/start", admin(),
                    MAPPER.createObjectNode().put("helmet", "REQUIRED").put("vest", "NOT_REQUIRED"), 200);
            assertThat(started.get("helmet").asString()).isEqualTo("REQUIRED");
            assertThat(started.get("vest").asString()).isEqualTo("NOT_REQUIRED");
            JsonNode policy = ppeDocument(tier0.await("config_bundle", latestVersion()));
            assertThat(policy.get("format_version").asString()).isEqualTo("1.2.0");
            assertThat(policy.get("work_id").longValue()).isEqualTo(work);
            assertThat(policy.get("helmet").asString()).isEqualTo("REQUIRED");
            assertThat(policy.get("vest").asString()).isEqualTo("NOT_REQUIRED");
            // 작업 중 다른 경로의 묶음도 그 작업의 요구를 싣는다
            assertThat(publish(null).at("/ppe_policy/vest").asString()).isEqualTo("NOT_REQUIRED");
            call("POST", "/api/work-units/" + work + "/end", admin(), null, 202);
        }
    }

    /** S15P31A101-669: 규칙 기준은 교실 시험으로 고정한다. 현장 기본값이 교실 시험이고, 프로필 없이 발행하면 교실 시험 판(R2·R3 v1001)을 싣는다 */
    @Test
    void 현장_규칙_기준의_기본은_교실_시험이고_프로필_없는_발행이_교실_시험_판을_싣는다() throws Exception {
        assertThat(jdbc.queryForObject("""
                SELECT column_default FROM information_schema.columns WHERE table_name = 'site' AND column_name = 'rule_profile'
                """, String.class)).startsWith("'CLASSROOM_TEST'");
        assertThat(call("GET", "/api/sites/current", viewer(), null, 200).get("rule_profile").asString()).isEqualTo("CLASSROOM_TEST");
        call("POST", "/api/calibrations", admin(), upload(calibration("cal-test-" + tag + "-p")), 201);
        JsonNode published = publish(null);
        assertThat(published.get("rule_profile").asString()).isEqualTo("CLASSROOM_TEST");
        assertThat(item(published, "R2").get("item_version").intValue()).isEqualTo(1001);
        assertThat(item(published, "R3").get("item_version").intValue()).isEqualTo(1001);
    }

    /**
     * S15P31A101-654: 현장 규칙 기준(site.rule_profile)을 교실 시험으로 바꾸면 그 기준(R3 v1001)으로 새 묶음을 내고, 이후 묶음(작업 시작·
     * 구역 저장·발행)이 모두 따른다. 이전 묶음의 프로필보다 현장 설정이 앞선다. 화면 선택은 없앴지만(-669) API 는 남아 있다
     */
    @Test
    void 현장_규칙_기준이_이후_모든_묶음의_규칙_판을_정한다() throws Exception {
        call("POST", "/api/calibrations", admin(), upload(calibration("cal-test-" + tag + "-r")), 201);
        // 기본은 교실 시험(-669). 바꾸기를 보려고 운영에서 시작한다
        jdbc.update("UPDATE site SET rule_profile = 'OPERATION'");
        assertThat(call("GET", "/api/sites/current", viewer(), null, 200).get("rule_profile").asString()).isEqualTo("OPERATION");
        call("PUT", "/api/sites/current/rule-profile", viewer(), MAPPER.createObjectNode().put("rule_profile", "CLASSROOM_TEST"), 403);
        assertThat(call("PUT", "/api/sites/current/rule-profile", admin(), MAPPER.createObjectNode().put("rule_profile", "FAST"), 400)
                .get("code").asString()).isEqualTo("INVALID_RULE_PROFILE");

        JsonNode changed = call("PUT", "/api/sites/current/rule-profile", admin(),
                MAPPER.createObjectNode().put("rule_profile", "CLASSROOM_TEST"), 200);
        assertThat(changed.get("rule_profile").asString()).isEqualTo("CLASSROOM_TEST");
        assertThat(changed.get("publish_error").isNull()).isTrue();
        JsonNode bundle = changed.get("bundle");
        bundles.add(bundle.get("config_version").asString());
        assertThat(bundle.get("rule_profile").asString()).isEqualTo("CLASSROOM_TEST");
        assertThat(item(bundle, "R3").get("item_version").intValue()).isEqualTo(1001);
        assertThat(item(bundle, "R2").get("item_version").intValue()).isEqualTo(1001);
        assertThat(call("GET", "/api/sites/current", viewer(), null, 200).get("rule_profile").asString()).isEqualTo("CLASSROOM_TEST");

        // 작업 시작 묶음과 구역 저장 묶음도 현장 기준을 따른다
        long work = createWork();
        call("POST", "/api/work-units/" + work + "/start", admin(), null, 200);
        String startVersion = latestVersion();
        JsonNode startView = call("GET", "/api/config-bundles/" + startVersion, viewer(), null, 200);
        assertThat(startView.get("rule_profile").asString()).isEqualTo("CLASSROOM_TEST");
        assertThat(startView.at("/ppe_policy/helmet").asString()).isEqualTo("REQUIRED");
        assertThat(item(startView, "R3").get("item_version").intValue()).isEqualTo(1001);
        ObjectNode commit = MAPPER.createObjectNode();
        commit.putObject("zone").put("op", "CREATE").set("body", zone("기준 시험 구역", 3.0));
        JsonNode committed = call("POST", "/api/zones/commit", admin(), commit, 200);
        assertThat(committed.at("/bundle/rule_profile").asString()).isEqualTo("CLASSROOM_TEST");
        call("POST", "/api/work-units/" + work + "/end", admin(), null, 202);

        // 현장 설정이 운영으로 바뀌면 직전 묶음(교실 시험)을 잇지 않고 운영 판(R3 v1)을 싣는다
        jdbc.update("UPDATE site SET rule_profile = 'OPERATION'");
        JsonNode operation = publish(null);
        assertThat(operation.get("rule_profile").asString()).isEqualTo("OPERATION");
        assertThat(item(operation, "R3").get("item_version").intValue()).isEqualTo(1);
    }

    /** 묶음을 만들지 못해도(보정 문서 없음) 현장 규칙 기준은 저장되고 publish_error 로 알린다(S15P31A101-654) */
    @Test
    void 묶음을_만들지_못해도_현장_규칙_기준은_저장된다() throws Exception {
        doReturn(Optional.of("cal-test-" + tag + "-not-uploaded")).when(calibrations).currentVersion();
        // 기본(교실 시험, -669)과 다른 값으로 바꿔야 저장을 확인할 수 있다
        JsonNode changed = call("PUT", "/api/sites/current/rule-profile", admin(),
                MAPPER.createObjectNode().put("rule_profile", "OPERATION"), 200);
        assertThat(changed.get("bundle").isNull()).isTrue();
        assertThat(changed.get("publish_error").asString()).isEqualTo("CURRENT_CALIBRATION_MISSING");
        assertThat(jdbc.queryForObject("SELECT rule_profile FROM site", String.class)).isEqualTo("OPERATION");
    }

    /** 묶음을 만들지 못하면(보정 문서 없음) 작업을 시작하지 않고 묶음도 남기지 않는다(S15P31A101-518 D1) */
    @Test
    void 묶음을_만들지_못하면_작업_시작을_되돌린다() throws Exception {
        long work = createWork();
        call("POST", "/api/calibrations", admin(), upload(calibration("cal-test-" + tag + "-f")), 201);
        // Tier 0 가 알린 지금 보정의 문서가 없는 상태: 묶음 만들기가 상태를 바꾼 뒤(같은 트랜잭션) 실패한다
        doReturn(Optional.of("cal-test-" + tag + "-not-uploaded")).when(calibrations).currentVersion();
        Integer before = jdbc.queryForObject("SELECT count(*) FROM config_bundle", Integer.class);
        JsonNode error = call("POST", "/api/work-units/" + work + "/start", admin(), null, 409);
        assertThat(error.get("code").asString()).isEqualTo("CURRENT_CALIBRATION_MISSING");
        assertThat(jdbc.queryForObject("SELECT status FROM work_unit WHERE work_id = ?", String.class, work)).isEqualTo("PLANNED");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle", Integer.class)).isEqualTo(before);
    }

    @Test
    void 다시_붙은_Tier0_에는_적용한_묶음도_다시_알리고_해시가_다른_APPLIED_는_적용으로_보지_않는다() throws Exception {
        call("POST", "/api/calibrations", admin(), upload(calibration("cal-test-" + tag + "-e")), 201);
        String version;
        String hash;
        try (Tier0 tier0 = tier0()) {
            tier0.send(health());
            Thread.sleep(300);
            version = publish(null).get("config_version").asString();
            hash = tier0.await("config_bundle", version).at("/payload/bundle_hash").asString();
            tier0.send(ack(version, hash));
            awaitStatus(version, "APPLIED");
        }
        // Tier 0 는 (재)연결 뒤 알림을 받기 전까지 UNVERIFIED 다. 적용했던 묶음이어도 다시 보낸다(주기 10초)
        try (Tier0 again = tier0()) {
            again.send(health());
            JsonNode resent = again.await("config_bundle", version, Duration.ofSeconds(15));
            assertThat(resent.at("/payload/bundle_hash").asString()).isEqualTo(hash);

            ObjectNode wrong = ack(version, "sha256:" + "0".repeat(64));
            again.send(wrong);
            JsonNode view = awaitStatus(version, "MISMATCH");
            assertThat(view.at("/last_ack/result").asString()).isEqualTo("APPLIED");
        }
    }

    @Test
    void 저장_뒤에_뭉개지는_높이와_꼭짓점_256개_초과는_거부한다() throws Exception {
        ObjectNode flat = zone("float 로 같아지는 높이", 3.0);
        flat.put("z_min_m", 1.0);
        flat.put("z_max_m", 1.00000001);
        assertThat(call("POST", "/api/zones", admin(), flat, 400).get("code").asString()).isEqualTo("INVALID_Z_RANGE");

        ObjectNode many = zone("꼭짓점 257", 3.0);
        ArrayNode ring = many.putArray("polygon_xy");
        for (int i = 0; i < 257; i++) {
            double angle = 2 * Math.PI * i / 257;
            ring.add(point(Math.cos(angle) * 5, Math.sin(angle) * 5));
        }
        assertThat(call("POST", "/api/zones", admin(), many, 400).get("code").asString()).isEqualTo("INVALID_POLYGON");

        ObjectNode tiny = podium(0.4);
        tiny.put("top_height_m", 1e-46);
        assertThat(call("POST", "/api/static-objects", admin(), tiny, 400).get("code").asString())
                .isEqualTo("INVALID_STATIC_OBJECT");
    }

    /**
     * S15P31A101-745: 세팅 「카메라 확인」을 실제로 통과한(PASSED + 같은 보정의 기준선) 보정과 그 보정을 그대로 다시 낸 재사용 보정만 묶음에
     * 통과 근거(config_bundle 1.4.0 components.calibration.verification)를 싣는다. 재생 시험 건너뜀·기준선 없는 PASSED·확인 없음은 싣지 않는다.
     */
    @Test
    void 카메라_확인을_통과한_세팅_보정만_묶음에_통과_근거를_싣는다() throws Exception {
        String passedCal = "cal-test-" + tag + "-cc-pass";
        String replayCal = "cal-test-" + tag + "-cc-replay";
        String handCal = "cal-test-" + tag + "-cc-hand";
        String reuseCal = "cal-test-" + tag + "-cc-reuse";
        String plainCal = "cal-test-" + tag + "-cc-none";
        for (String cal : List.of(passedCal, replayCal, handCal, reuseCal, plainCal)) {
            call("POST", "/api/calibrations", admin(), upload(calibration(cal)), 201);
        }
        String site = jdbc.queryForObject("SELECT site_id FROM calibration_version WHERE calibration_version = ?", String.class, passedCal);
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, current_date, ?, now(), now() + interval '1 hour', 2, 'PLANNED', ?, now(), now())
                """, site, workName(), adminId);
        long work = jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, workName());
        UUID passed = UUID.randomUUID();
        List<UUID> sessions = new ArrayList<>();
        String scene = "scene-test-" + tag + "-cc";
        jdbc.update("""
                INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, is_upper_bound, committed_at, valid_from)
                VALUES (?, ?, ?, 'COMMITTED', ?, false, now(), now())
                """, scene, site, passedCal, "scenes/" + scene + ".glb");
        try {
            sessions.add(setupSession(passed, site, work, passedCal, "PASSED", true, null, null));
            sessions.add(setupSession(UUID.randomUUID(), site, work, replayCal, "SKIPPED_REPLAY", false, null, null));
            sessions.add(setupSession(UUID.randomUUID(), site, work, handCal, "PASSED", false, null, null)); // 손으로 고친 PASSED(17차)
            sessions.add(setupSession(UUID.randomUUID(), site, work, reuseCal, null, false, passed, scene)); // 이전 맵 재사용(-714)

            JsonNode verification = MAPPER.createObjectNode().put("camera_check", "PASSED").put("calibration_version", passedCal)
                    .put("setup_session_id", passed.toString());
            assertThat(calibrationRef(passedCal).get("verification")).isEqualTo(verification);
            assertThat(calibrationRef(reuseCal).get("verification"))
                    .isEqualTo(((ObjectNode) verification.deepCopy()).put("calibration_version", reuseCal));
            for (String cal : List.of(replayCal, handCal, plainCal)) {
                assertThat(calibrationRef(cal).has("verification")).as(cal).isFalse();
            }

            // Tier 0 가 받는 알림(1.4.0)이 계약에 맞고 매니페스트의 components 와 같다
            doReturn(Optional.of(passedCal)).when(calibrations).currentVersion();
            try (Tier0 tier0 = tier0()) {
                tier0.send(health());
                Thread.sleep(300);
                String version = publish(null).get("config_version").asString();
                JsonNode notice = tier0.await("config_bundle", version);
                assertThat(notice.get("schema_version").asString()).isEqualTo("1.4.0");
                assertThat(schemaViolations("config_bundle", notice)).isEmpty();
                assertThat(notice.at("/payload/components/calibration/verification")).isEqualTo(verification);
                JsonNode manifest = MAPPER.readTree(download(notice.at("/payload/bundle_uri").asString()));
                assertThat(manifest.get("components")).isEqualTo(notice.at("/payload/components"));
            }
        } finally {
            for (UUID session : sessions.reversed()) {
                jdbc.update("DELETE FROM setup_session WHERE setup_session_id = ?", session);
                jdbc.update("DELETE FROM analysis_job WHERE job_id = ?", "job-cc-" + session);
            }
            jdbc.update("DELETE FROM scene_version WHERE scene_version = ?", scene);
        }
    }

    /** 이 보정 버전을 지금 보정으로 둔 발행 묶음의 components.calibration */
    private JsonNode calibrationRef(String calibrationVersion) throws Exception {
        doReturn(Optional.of(calibrationVersion)).when(calibrations).currentVersion();
        String version = publish(null).get("config_version").asString();
        assertThat(jdbc.queryForObject("SELECT calibration_version FROM config_bundle WHERE config_version = ?", String.class, version))
                .isEqualTo(calibrationVersion);
        return MAPPER.readTree(jdbc.queryForObject("SELECT components::text FROM config_bundle WHERE config_version = ?", String.class,
                version)).get("calibration");
    }

    /** 세팅 세션 한 건. checkStatus 가 null 이면 카메라 확인 기록이 없다. baseline 은 워커가 PASSED 에만 남기는 검증 기준선 */
    private UUID setupSession(UUID id, String site, long work, String calibrationVersion, String checkStatus, boolean baseline,
            UUID reuseSource, String reuseScene) {
        // 재사용 세션은 워커 작업이 없고 고른 맵·시점 확인을 남긴다(V78 setup_session_reuse_source_check)
        String job = reuseSource == null ? "job-cc-" + id : null;
        if (job != null) {
            jdbc.update("INSERT INTO analysis_job(job_id,job_type,priority,status,attempt,max_attempts,timeout_ms,input,created_at)"
                    + " VALUES (?, 'site_setup','BACKGROUND','SUCCEEDED',1,1,1000,'{}'::jsonb,now())", job);
        }
        String mode = reuseSource == null ? "NEW" : "REUSE";
        ObjectNode check = null;
        if (checkStatus != null) {
            check = MAPPER.createObjectNode().put("status", checkStatus).put("calibration_version", calibrationVersion);
            if (baseline) {
                check.putObject("baseline").put("calibration_version", calibrationVersion).put("source", "SETUP_CAMERA_CHECK")
                        .putObject("values_px");
            }
        }
        jdbc.update("""
                INSERT INTO setup_session (setup_session_id, site_id, work_unit_id, job_id, status, progress_pct, camera_heights_m,
                    height_tolerance_cm, requested_by, started_at, completed_at, updated_at, calibration_version, camera_check,
                    reuse_source_session_id, reuse_scene_version, reuse_view_check, mode, requested_mode)
                VALUES (?, ?, ?, ?, 'COMPLETED', 100, '{}'::jsonb, 5, ?, now(), now(), now(), ?, CAST(? AS jsonb), ?, ?,
                    CAST(? AS jsonb), ?, ?)
                """, id, site, work, job, adminId, calibrationVersion, check == null ? null : check.toString(), reuseSource, reuseScene,
                reuseSource == null ? null : "{}", mode, mode);
        return id;
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

    @Test
    void 보관한_구역과_작업면은_목록과_다음_묶음에서_빠지고_이력은_남는다() throws Exception {
        call("POST", "/api/calibrations", admin(), upload(calibration("cal-test-" + tag + "-e")), 201);
        String zoneId = call("POST", "/api/zones", admin(), zone("잘못 만든 구역", 3.0), 201).get("zone_id").asString();
        String surfaceId = call("POST", "/api/static-objects", admin(), podium(0.4), 201).get("object_id").asString();
        JsonNode before = publish(null);
        assertThat(itemIds(before)).contains(zoneId, surfaceId);

        call("DELETE", "/api/zones/" + zoneId, viewer(), null, 403);
        call("DELETE", "/api/zones/" + zoneId, admin(), null, 204);
        call("DELETE", "/api/static-objects/" + surfaceId, admin(), null, 204);

        assertThat(call("GET", "/api/zones", viewer(), null, 200).valueStream().map(z -> z.get("zone_id").asString()))
                .doesNotContain(zoneId);
        assertThat(call("GET", "/api/static-objects", viewer(), null, 200).valueStream()
                .map(o -> o.get("object_id").asString())).doesNotContain(surfaceId);
        JsonNode history = call("GET", "/api/zones/" + zoneId + "/versions", viewer(), null, 200);
        assertThat(history).hasSize(1);
        assertThat(history.get(0).get("valid_to").isNull()).isFalse();

        // 보관한 항목은 고칠 수 없다(새 ID 로 다시 만든다). 다시 보관하는 것은 성공(두 번 누름), 없는 ID 는 404
        assertThat(call("PUT", "/api/zones/" + zoneId, admin(), zone("되살림", 3.0), 404).get("code").asString())
                .isEqualTo("ZONE_NOT_FOUND");
        call("DELETE", "/api/zones/" + zoneId, admin(), null, 204);
        call("DELETE", "/api/static-objects/" + surfaceId, admin(), null, 204);
        assertThat(call("PUT", "/api/static-objects/" + surfaceId, admin(), podium(0.5), 404).get("code").asString())
                .isEqualTo("STATIC_OBJECT_NOT_FOUND");
        call("DELETE", "/api/zones/z-none", admin(), null, 404);
        call("DELETE", "/api/static-objects/so-none", admin(), null, 404);

        JsonNode after = publish(null);
        assertThat(itemIds(after)).doesNotContain(zoneId, surfaceId);
        // 이미 발행한 묶음의 항목 목록은 그대로다
        assertThat(itemIds(call("GET", "/api/config-bundles/" + before.get("config_version").asString(), viewer(), null, 200)))
                .contains(zoneId, surfaceId);
    }

    @Test
    void 묶음_조회는_실은_구역_규칙_작업면의_ID_와_버전을_싣는다() throws Exception {
        call("POST", "/api/calibrations", admin(), upload(calibration("cal-test-" + tag + "-f")), 201);
        String zoneId = call("POST", "/api/zones", admin(), zone("양중 하부", 3.0), 201).get("zone_id").asString();
        call("PUT", "/api/zones/" + zoneId, admin(), zone("양중 하부(넓힘)", 4.0), 200);
        String surfaceId = call("POST", "/api/static-objects", admin(), podium(0.4), 201).get("object_id").asString();
        JsonNode published = publish(null);
        String version = published.get("config_version").asString();

        for (JsonNode bundle : List.of(published, call("GET", "/api/config-bundles/" + version, viewer(), null, 200),
                call("GET", "/api/config-bundles", viewer(), null, 200).valueStream()
                        .filter(b -> b.get("config_version").asString().equals(version)).findFirst().orElseThrow())) {
            JsonNode zoneItem = item(bundle, zoneId);
            assertThat(zoneItem.get("item_type").asString()).isEqualTo("ZONE");
            assertThat(zoneItem.get("item_version").intValue()).isEqualTo(2);
            assertThat(zoneItem.has("kind")).isFalse();
            JsonNode surfaceItem = item(bundle, surfaceId);
            assertThat(surfaceItem.get("item_type").asString()).isEqualTo("STATIC_OBJECT");
            assertThat(surfaceItem.get("item_version").intValue()).isEqualTo(1);
            assertThat(surfaceItem.get("kind").asString()).isEqualTo("WORK_SURFACE");
            assertThat(bundle.get("items").valueStream().filter(i -> i.get("item_type").asString().equals("RULE"))).isNotEmpty();
        }
    }

    // ---- 도우미

    private static List<String> itemIds(JsonNode bundle) {
        return bundle.get("items").valueStream().map(i -> i.get("item_id").asString()).toList();
    }

    private static JsonNode item(JsonNode bundle, String itemId) {
        return bundle.get("items").valueStream().filter(i -> i.get("item_id").asString().equals(itemId)).findFirst().orElseThrow();
    }

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

    private List<String> dataViolations(String name, JsonNode doc) {
        return SCHEMAS.getSchema(SchemaLocation.of("https://simai.dev/contracts/data/" + name + ".schema.json"))
                .validate(MAPPER.writeValueAsString(doc), InputFormat.JSON).stream().map(Object::toString).toList();
    }

    /** 알림의 ppe_policy 를 Tier 0 처럼 받아 해시·버전·계약을 맞춰 본 문서 */
    private JsonNode ppeDocument(JsonNode notice) throws Exception {
        JsonNode ref = notice.at("/payload/components/ppe_policy");
        byte[] bytes = download(ref.get("uri").asString());
        assertThat(ConfigFiles.sha256(bytes)).isEqualTo(ref.get("sha256").asString());
        JsonNode doc = MAPPER.readTree(bytes);
        assertThat(doc.get("version").asString()).isEqualTo(ref.get("version").asString());
        assertThat(dataViolations("ppe_policy", doc)).isEmpty();
        return doc;
    }

    private String latestVersion() {
        return jdbc.queryForObject("""
                SELECT config_version FROM config_bundle WHERE config_version ~ '^cfg-[0-9]+$'
                ORDER BY CAST(substring(config_version FROM 5) AS integer) DESC LIMIT 1
                """,
                String.class);
    }

    private String workName() {
        return "ppe-work-" + tag;
    }

    private long createWork() throws Exception {
        long now = Instant.now().toEpochMilli();
        ObjectNode body = MAPPER.createObjectNode();
        body.put("date", Instant.now().atOffset(java.time.ZoneOffset.UTC).toLocalDate().toString());
        body.put("name", workName());
        body.put("from_ms", now - 3_600_000).put("to_ms", now + 3_600_000);
        body.put("planned_people", 2).put("work_type", "OPENING_WORK");
        long id = Long.parseLong(call("POST", "/api/work-units", admin(), body, 201).get("work_id").asString());
        // 작업 시작은 세팅 완료 뒤다(S15P31A101-642). 묶음 시험이라 세팅을 마친 작업으로 둔다
        jdbc.update("UPDATE work_unit SET setup_completed_at = now() WHERE work_id = ?", id);
        return id;
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
        return new Tier0(session, received,
                () -> i1Connections.all().stream().noneMatch(c -> gatewayId.equals(c.gatewayId())));
    }

    /**
     * Tier 0 흉내. EC2 가 보낸 메시지를 모은다. 닫을 때는 EC2 가 이 연결을 닫았다고 볼 때(afterConnectionClosed)까지 기다린다.
     * EC2 는 한 연결의 프레임을 차례로 동기 처리하므로, 그때는 앞서 보낸 메시지(예: 같은 msg_id 로 다시 보낸 config_ack)의
     * 처리가 끝났다. 기다리지 않으면 늦게 처리된 중복 ack 가 정리 단계가 config_ack 를 지운 뒤 다시 들어가
     * config_bundle 삭제가 FK 위반으로 깨진다(S15P31A101-775, Jenkins #1106).
     */
    private record Tier0(WebSocketSession session, BlockingQueue<JsonNode> received, BooleanSupplier serverClosed)
            implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(MAPPER.writeValueAsString(message)));
        }

        JsonNode await(String msgType, String configVersion) throws InterruptedException {
            return await(msgType, configVersion, Duration.ofSeconds(10));
        }

        JsonNode await(String msgType, String configVersion, Duration timeout) throws InterruptedException {
            long deadline = System.nanoTime() + timeout.toNanos();
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
            long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
            while (!serverClosed.getAsBoolean()) {
                if (System.nanoTime() > deadline) {
                    throw new AssertionError("EC2 가 I1 연결 닫기를 처리하지 않았다");
                }
                Thread.sleep(20);
            }
        }
    }
}
