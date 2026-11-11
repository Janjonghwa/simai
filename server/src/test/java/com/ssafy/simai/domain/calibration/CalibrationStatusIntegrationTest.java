package com.ssafy.simai.domain.calibration;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import org.assertj.core.data.Offset;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;
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
import org.springframework.web.socket.handler.AbstractWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * Tier 0 보정 상태 알림(I1 calibration_update)과 보정 상태·설치 안내 조회(S15P31A101-263, 명세 4.4·4.5, FR-25·26).
 * 실제 포트로 Tier 0(I1)처럼 보내고 GET /api/calibrations/current 가 가장 늦게 알린 상태를 주는지 본다.
 * 현재 보정은 표 전체에서 sent_at 이 가장 늦은 알림이라, 시험은 지금보다 뒤 시각으로 보내 다른 행보다 늦게 만든다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class CalibrationStatusIntegrationTest {

    /** application-test.yaml 의 simai.tier0.token-sha256 원문 */
    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final String METRIC = "simai.i1.messages";
    /** 계약 예시 calibration.json 의 content_hash(Python 으로 잰 값, CalibrationContentHashTest). 같은 내용의 문서를 올리면 이 해시가 된다 */
    private static final String EXAMPLE_HASH = "sha256:75ffde54159aff5cf3cc892429f920c2b3d12ce45c3b35af89eb6f54393ae846";

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    MeterRegistry meters;

    @Autowired
    ContractSchemas schemas;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String gatewayId = "tier0-cal-" + tag;
    private final String prefix = "cal-test263-" + tag;
    /** 지금보다 이른 Tier 0 시각. 현재 보정 순서는 LEAST(sent_at, received_at) 라 미래 시각은 받은 시각으로 눌린다 */
    private final long base = Instant.now().minus(Duration.ofMinutes(10)).toEpochMilli();
    private final List<String> bundles = new ArrayList<>();
    private long adminId;

    @BeforeEach
    void users() {
        adminId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id", Long.class, "cal-admin-" + tag);
    }

    @AfterEach
    void cleanUp() {
        String like = prefix + "%";
        for (String version : bundles) {
            jdbc.update("DELETE FROM config_ack WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle_file WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", version);
            jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", version);
        }
        jdbc.update("DELETE FROM scene_version WHERE scene_version LIKE ?", like);
        jdbc.update("DELETE FROM calibration_update_message WHERE calibration_version LIKE ?", like);
        jdbc.update("DELETE FROM coverage_guide WHERE calibration_version LIKE ?", like);
        jdbc.update("DELETE FROM calibration_document WHERE calibration_version LIKE ?", like);
        // 이전 버전을 가리키는 행부터 지운다
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version LIKE ? AND previous_calibration_version IS NOT NULL", like);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version LIKE ?", like);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 알림을_받으면_현재_보정이_그_상태와_버전과_카메라별_오차를_돌려준다() throws Exception {
        ObjectNode first = update(prefix + "-a", null, "PROVISIONAL", "INITIAL", base);

        try (Tier0 tier0 = tier0()) {
            tier0.send(first);
        }

        awaitCount(() -> messages(prefix + "-a"), 1);
        JsonNode current = call("/api/calibrations/current", viewer(), 200);
        assertThat(current.get("calibration_state").asString()).isEqualTo("PROVISIONAL");
        assertThat(current.get("calibration_version").asString()).isEqualTo(prefix + "-a");
        assertThat(current.get("provider_source").asString()).isEqualTo("G1_AUTO");
        assertThat(current.get("reason").asString()).isEqualTo("INITIAL");
        assertThat(current.get("updated_at").asLong()).isEqualTo(base);
        assertThat(current.get("effective_at").asLong()).isEqualTo(base + 600);
        assertThat(current.get("cameras").get(0).get("camera_id").asString()).isEqualTo("cam2");
        assertThat(current.get("cameras").get(0).at("/reprojection_error_px/p95").asDouble()).isEqualTo(8.9);
        assertThat(current.get("document_uploaded").asBoolean()).isFalse();
        assertThat(current.get("camera_poses").isArray()).isTrue();
        assertThat(current.get("camera_poses")).isEmpty();
        assertThat(schemas.violations("messages/calibration_status", MAPPER.writeValueAsString(current))).isEmpty();
        assertThat(jdbc.queryForObject("SELECT state FROM calibration_version WHERE calibration_version = ?", String.class,
                prefix + "-a")).isEqualTo("PROVISIONAL");
    }

    @Test
    void DRIFTED_는_같은_버전의_현재_상태만_바꾸고_늦게_온_옛_알림은_현재가_되지_않는다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "CALIBRATED", "INITIAL", base));
            awaitCount(() -> messages(prefix + "-a"), 1);
            tier0.send(update(prefix + "-a", null, "DRIFTED", "DRIFT_DETECTED", base + 2_000));
            awaitCount(() -> messages(prefix + "-a"), 2);
            // 재전송이 늦게 도착: Tier 0 시계로 더 이른 알림
            tier0.send(update(prefix + "-a", null, "PROVISIONAL", "STATE_CHANGED", base + 1_000));
        }

        awaitCount(() -> messages(prefix + "-a"), 3);
        JsonNode current = call("/api/calibrations/current", viewer(), 200);
        assertThat(current.get("calibration_state").asString()).isEqualTo("DRIFTED");
        assertThat(current.get("reason").asString()).isEqualTo("DRIFT_DETECTED");
        assertThat(current.get("updated_at").asLong()).isEqualTo(base + 2_000);
        // 버전 행은 덮어쓰지 않는다(처음 알린 값 그대로)
        assertThat(jdbc.queryForObject("SELECT state FROM calibration_version WHERE calibration_version = ?", String.class,
                prefix + "-a")).isEqualTo("CALIBRATED");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM calibration_version WHERE calibration_version LIKE ?", Integer.class,
                prefix + "%")).isEqualTo(1);
    }

    @Test
    void Tier0_시계가_한_번_미래로_앞선_알림은_시계를_바로잡은_뒤_알림에_현재를_넘긴다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            // 시계 동기화 전: 하루 뒤로 찍힌 알림
            tier0.send(update(prefix + "-a", null, "DRIFTED", "DRIFT_DETECTED", Instant.now().plus(Duration.ofDays(1)).toEpochMilli()));
            awaitCount(() -> messages(prefix + "-a"), 1);
            Thread.sleep(20);
            // 시계를 바로잡은 뒤의 알림(보낸 시각은 앞 알림보다 이르다)
            tier0.send(update(prefix + "-a", null, "CALIBRATED", "STATE_CHANGED", Instant.now().toEpochMilli()));
        }

        awaitCount(() -> messages(prefix + "-a"), 2);
        assertThat(call("/api/calibrations/current", viewer(), 200).get("calibration_state").asString()).isEqualTo("CALIBRATED");
    }

    @Test
    void 새_버전은_이전_버전을_가리키고_재보정_뒤_현재가_된다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "CALIBRATED", "INITIAL", base));
            awaitCount(() -> messages(prefix + "-a"), 1);
            ObjectNode next = update(prefix + "-b", prefix + "-a", "CALIBRATED", "RECALIBRATED", base + 1_000);
            ((ObjectNode) next.get("payload")).put("calibration_hash", "sha256:" + "2".repeat(64));
            tier0.send(next);
        }

        awaitCount(() -> messages(prefix + "-b"), 1);
        JsonNode current = call("/api/calibrations/current", viewer(), 200);
        assertThat(current.get("calibration_version").asString()).isEqualTo(prefix + "-b");
        assertThat(current.get("previous_calibration_version").asString()).isEqualTo(prefix + "-a");
        assertThat(jdbc.queryForObject("SELECT previous_calibration_version FROM calibration_version WHERE calibration_version = ?",
                String.class, prefix + "-b")).isEqualTo(prefix + "-a");
    }

    @Test
    void 같은_msg_id_는_한_번만_저장하고_같은_버전의_다른_해시와_계약_위반은_버린다() throws Exception {
        ObjectNode first = update(prefix + "-a", null, "CALIBRATED", "INITIAL", base);
        ObjectNode otherHash = update(prefix + "-a", null, "DRIFTED", "DRIFT_DETECTED", base + 1_000);
        ((ObjectNode) otherHash.get("payload")).put("calibration_hash", "sha256:" + "3".repeat(64));
        // 공개 데이터 정답 보정은 CALIBRATED 가 될 수 없다(계약)
        ObjectNode simCalibrated = update(prefix + "-a", null, "CALIBRATED", "STATE_CHANGED", base + 2_000);
        ((ObjectNode) simCalibrated.get("payload")).put("provider_source", "SIM");
        double duplicateBefore = counter("duplicate");
        double invalidBefore = counter("invalid");

        // 같은 msg_id 가 다른 버전으로 와도 중복이다(버전 행을 만들지 않는다)
        ObjectNode sameIdOtherVersion = first.deepCopy();
        ((ObjectNode) sameIdOtherVersion.get("payload")).put("calibration_version", prefix + "-z");

        try (Tier0 tier0 = tier0()) {
            tier0.send(first);
            tier0.send(first);
            tier0.send(sameIdOtherVersion);
            tier0.send(otherHash);
            tier0.send(simCalibrated);
        }

        awaitCount(() -> (int) (counter("invalid") - invalidBefore), 2);
        assertThat(counter("duplicate") - duplicateBefore).isEqualTo(2.0);
        assertThat(messages(prefix + "-a")).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM calibration_version WHERE calibration_version = ?", Integer.class,
                prefix + "-z")).isZero();
        assertThat(call("/api/calibrations/current", viewer(), 200).get("calibration_state").asString()).isEqualTo("CALIBRATED");
    }

    @Test
    void Tier0_가_알린_버전에_같은_내용의_문서를_올리면_받고_다른_내용은_해시를_맞춰_적어도_거부한다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "CALIBRATED", "INITIAL", base));
            ObjectNode other = update(prefix + "-b", prefix + "-a", "CALIBRATED", "RECALIBRATED", base + 1_000);
            ((ObjectNode) other.get("payload")).put("calibration_hash", "sha256:" + "4".repeat(64));
            tier0.send(other);
        }
        awaitCount(() -> messages(prefix + "-b"), 1);
        assertThat(call("/api/calibrations/current", viewer(), 200).get("document_uploaded").asBoolean()).isFalse();

        // 원문 해시가 아니라 다시 잰 content_hash(버전·시각·상태를 뺀 내용)로 맞춘다. 문서가 적은 content_hash 는 무시한다
        call("POST", "/api/calibrations", admin(), upload(document(prefix + "-a", "sha256:" + "9".repeat(64))), 201);
        // 내용이 다른 문서가 Tier 0 의 해시를 적어 와도 다시 재서 거부한다
        assertThat(call("POST", "/api/calibrations", admin(), upload(document(prefix + "-b", "sha256:" + "4".repeat(64))), 409)
                .get("code").asString()).isEqualTo("CALIBRATION_VERSION_CONFLICT");
        JsonNode current = call("/api/calibrations/current", viewer(), 200);
        assertThat(current.get("calibration_version").asString()).isEqualTo(prefix + "-b");
        assertThat(current.get("document_uploaded").asBoolean()).isFalse();
        assertThat(current.get("latest_uploaded_version").asString()).isEqualTo(prefix + "-a");
    }

    @Test
    void 업로드로_먼저_만든_버전은_고치지_않고_이전_버전은_Tier0_알림_값으로_준다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "CALIBRATED", "INITIAL", base));
        }
        awaitCount(() -> messages(prefix + "-a"), 1);
        ObjectNode doc = document(prefix + "-b", null);
        ((ObjectNode) doc.get("cameras").get(0)).put("camera_height_m", 2.75);
        call("POST", "/api/calibrations", admin(), upload(doc), 201);
        assertThat(previous(prefix + "-b")).isNull();
        String uploadedHash = jdbc.queryForObject("SELECT calibration_hash FROM calibration_version WHERE calibration_version = ?",
                String.class, prefix + "-b");

        try (Tier0 tier0 = tier0()) {
            ObjectNode next = update(prefix + "-b", prefix + "-a", "PROVISIONAL", "RECALIBRATED", base + 1_000);
            ((ObjectNode) next.get("payload")).put("calibration_hash", uploadedHash);
            tier0.send(next);
        }

        awaitCount(() -> messages(prefix + "-b"), 1);
        // 버전 행은 덮어쓰지 않는다(AGENTS 6): 처음 만든 값(이전 버전 없음, 업로드 문서 주소) 그대로
        assertThat(previous(prefix + "-b")).isNull();
        assertThat(jdbc.queryForObject("SELECT calibration_ref FROM calibration_version WHERE calibration_version = ?",
                String.class, prefix + "-b")).isEqualTo("ec2:calibration_document/" + prefix + "-b");
        JsonNode current = call("/api/calibrations/current", viewer(), 200);
        assertThat(current.get("previous_calibration_version").asString()).isEqualTo(prefix + "-a");
        assertThat(current.get("document_uploaded").asBoolean()).isTrue();
    }

    @Test
    void 예전에_원문_해시로_남긴_업로드_행은_고치지_않고_올린_문서가_같은_내용이면_받는다() throws Exception {
        String version = prefix + "-a";
        byte[] content = MAPPER.writeValueAsBytes(document(version, null));
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, 'classroom-1', 'PROVISIONAL', 'G1_AUTO', '관리자 업로드', now(), ?, 'sha256:raw-bytes', now())
                """, version, "ec2:calibration_document/" + version);
        jdbc.update("""
                INSERT INTO calibration_document (calibration_version, content, sha256, uploaded_by, uploaded_at)
                VALUES (?, ?, 'sha256:raw-bytes', ?, now())
                """, version, content, adminId);

        try (Tier0 tier0 = tier0()) {
            tier0.send(update(version, null, "CALIBRATED", "STATE_CHANGED", base));
        }

        awaitCount(() -> messages(version), 1);
        assertThat(jdbc.queryForObject("SELECT calibration_hash FROM calibration_version WHERE calibration_version = ?",
                String.class, version)).isEqualTo("sha256:raw-bytes");
        assertThat(call("/api/calibrations/current", viewer(), 200).get("calibration_state").asString()).isEqualTo("CALIBRATED");
    }

    @Test
    void 브라우저가_실수를_정수로_바꾼_문서는_해시가_달라_거부되고_글자_그대로_보내면_받는다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "CALIBRATED", "INITIAL", base));
        }
        awaitCount(() -> messages(prefix + "-a"), 1);
        String original = Files.readString(CONTRACTS.resolve("examples/calibration.json"));
        String text = original.replace(MAPPER.readTree(original).get("calibration_version").asString(), prefix + "-a");
        assertThat(text).contains(prefix + "-a").contains("1000.0");
        // JSON.parse → stringify 를 거치면 1000.0 이 1000 이 된다
        ObjectNode viaBrowser = (ObjectNode) MAPPER.readTree(text.replace("1000.0", "1000").replace("960.0", "960"));

        assertThat(call("POST", "/api/calibrations", admin(), upload(viaBrowser), 409).get("code").asString())
                .isEqualTo("CALIBRATION_VERSION_CONFLICT");
        ObjectNode body = MAPPER.createObjectNode();
        body.put("document_text", text);
        call("POST", "/api/calibrations", admin(), body, 201);
        // 둘 다 보내면 거부
        body.set("document", viaBrowser);
        call("POST", "/api/calibrations", admin(), body, 400);
        // 글자 그대로 저장한다(구성 묶음이 이 글자를 싣는다)
        assertThat(new String(jdbc.queryForObject("SELECT content FROM calibration_document WHERE calibration_version = ?",
                byte[].class, prefix + "-a"), StandardCharsets.UTF_8)).isEqualTo(text);
    }

    @Test
    void 구성_묶음은_Tier0_가_알린_현재_버전의_문서를_싣고_없으면_만들지_않는다() throws Exception {
        call("POST", "/api/calibrations", admin(), upload(document(prefix + "-b", null)), 201);
        try (Tier0 tier0 = tier0()) {
            ObjectNode a = update(prefix + "-a", null, "CALIBRATED", "INITIAL", base);
            ((ObjectNode) a.get("payload")).put("calibration_hash", "sha256:" + "6".repeat(64));
            tier0.send(a);
        }
        awaitCount(() -> messages(prefix + "-a"), 1);

        // 가장 최근에 올린 문서(-b)가 아니라 현재 버전(-a)의 문서가 있어야 한다
        assertThat(call("POST", "/api/config-bundles", admin(), MAPPER.createObjectNode(), 409).get("code").asString())
                .isEqualTo("CURRENT_CALIBRATION_MISSING");

        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-b", prefix + "-a", "CALIBRATED", "RECALIBRATED", base + 1_000));
        }
        awaitCount(() -> messages(prefix + "-b"), 1);
        JsonNode published = call("POST", "/api/config-bundles", admin(), MAPPER.createObjectNode(), 201);
        bundles.add(published.get("config_version").asString());
        assertThat(published.get("calibration_version").asString()).isEqualTo(prefix + "-b");
    }

    @Test
    void 설치_안내는_현재_보정_버전의_안내를_관리자에게만_준다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(update(prefix + "-a", null, "PROVISIONAL", "INITIAL", base));
            awaitCount(() -> messages(prefix + "-a"), 1);
            ObjectNode next = update(prefix + "-b", prefix + "-a", "CALIBRATED", "RECALIBRATED", base + 1_000);
            ((ObjectNode) next.get("payload")).put("calibration_hash", "sha256:" + "5".repeat(64));
            tier0.send(next);
        }
        awaitCount(() -> messages(prefix + "-b"), 1);
        guide(prefix + "-a", "cam1", "이전 보정의 안내");
        guide(prefix + "-b", "cam3", "카메라 3번이 너무 아래를 봄");
        guide(prefix + "-b", null, "남쪽 벽을 따라 한 번 더 걸어 주세요");

        JsonNode body = call("/api/cameras/coverage-guides", admin(), 200);

        assertThat(body.get("calibration_version").asString()).isEqualTo(prefix + "-b");
        assertThat(body.get("guides")).hasSize(2);
        assertThat(body.get("guides").get(0).get("camera_id").asString()).isEqualTo("cam3");
        assertThat(body.get("guides").get(0).get("message").asString()).isEqualTo("카메라 3번이 너무 아래를 봄");
        assertThat(body.get("guides").get(0).at("/metrics/floor_coverage_ratio").asDouble()).isEqualTo(0.42);
        assertThat(body.get("guides").get(1).get("camera_id").isNull()).isTrue();
        call("/api/cameras/coverage-guides", viewer(), 403);
    }

    @Autowired
    com.ssafy.simai.domain.scene.SceneCompatibility sceneCompatibility;

    @Autowired
    com.ssafy.simai.domain.health.HealthState healthState;

    @Autowired
    com.ssafy.simai.domain.health.ViewerHealthService viewerHealth;

    @Test
    void I1_자세와_World계보를_저장하고_Scene생성보정_대비_누적변화를_판정한다() throws Exception {
        String root = prefix + "-a";
        try (Tier0 tier0 = tier0()) {
            String previous = null;
            for (String suffix : List.of("a", "b", "c", "d")) {
                String version = prefix + "-" + suffix;
                var message = update(version, previous, "PROVISIONAL", previous == null ? "INITIAL" : "RECALIBRATED", base);
                message.put("schema_version", "2.1.0");
                var p = (ObjectNode) message.get("payload");
                p.put("world_frame_id", suffix.equals("d") ? "another-world" : root);
                var pose = p.putArray("camera_poses").addObject().put("camera_id", "cam_A");
                pose.set("R", MAPPER.valueToTree(new int[][] {{1,0,0},{0,1,0},{0,0,1}}));
                double x = suffix.equals("b") ? .06 : suffix.equals("c") ? .12 : 0;
                pose.set("t", MAPPER.valueToTree(new double[] {-x,0,-2}));
                tier0.send(message);
                awaitCount(() -> messages(version), 1);
                previous = version;
            }
            var small = sceneCompatibility.compare("scene-lineage", root, prefix + "-b");
            assertThat(small.path("verification").asString()).isEqualTo("VERIFIED");
            var large = sceneCompatibility.compare("scene-lineage", root, prefix + "-c");
            assertThat(large.path("verification").asString()).isEqualTo("DIMMED");
            assertThat(large.path("regeneration_required").asBoolean()).isTrue();
            assertThat(large.path("max_center_delta_m").asDouble()).isEqualTo(.12);
            assertThat(sceneCompatibility.compare("scene-lineage", root, prefix + "-d").path("verification").asString())
                    .isEqualTo("MISMATCH");

            String scene = prefix + "-scene";
            jdbc.update("""
                    INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, is_upper_bound,
                        committed_at, valid_from)
                    VALUES (?, 'classroom-1', ?, 'COMMITTED', 'scenes/lineage.glb', false, now() + interval '1 day', now())
                    """, scene, root);
            for (String suffix : List.of("b", "c", "d")) {
                String version = prefix + "-" + suffix;
                ObjectNode world = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples/world_state.json")));
                long now = Instant.now().toEpochMilli();
                world.put("msg_id", UUID.randomUUID().toString());
                world.put("source_id", gatewayId);
                world.put("sent_at", now);
                ObjectNode p = (ObjectNode) world.get("payload");
                p.put("t_latest_global", now);
                p.put("scene_version", scene);
                p.put("calibration_version", version);
                p.putArray("tracks");
                p.remove("input_provenance");
                p.remove("config_version");
                tier0.send(world);
                awaitCount(() -> healthState.world() != null && version.equals(healthState.world().calibrationVersion()) ? 1 : 0, 1);
                JsonNode rest = call("/api/scenes/current", viewer(), 200);
                JsonNode health = viewerHealth.message().at("/payload/scene/compatibility");
                // REST 는 카메라별 값(cameras, S15P31A101-602)을 더 싣고 viewer_health 는 싣지 않는다. 나머지는 같다
                ObjectNode restCompatibility = ((ObjectNode) rest.get("compatibility")).deepCopy();
                JsonNode cameras = restCompatibility.remove("cameras");
                assertThat(restCompatibility).isEqualTo(health);
                assertThat(health.has("cameras")).isFalse();
                if (suffix.equals("d")) {
                    assertThat(cameras.isNull()).isTrue();
                } else {
                    assertThat(cameras).hasSize(1);
                    assertThat(cameras.get(0).path("center_delta_m").asDouble())
                            .isEqualTo(health.path("max_center_delta_m").asDouble());
                }
                assertThat(health.path("scene_version").asString()).isEqualTo(scene);
                assertThat(health.path("calibration_version").asString()).isEqualTo(version);
                assertThat(health.path("verification").asString()).isEqualTo(
                        suffix.equals("b") ? "VERIFIED" : suffix.equals("c") ? "DIMMED" : "MISMATCH");
                assertThat(rest.path("calibration_state").asString()).isEqualTo("PROVISIONAL");
            }
        }
    }

    @Test
    void 현재_보정_문서가_있으면_카메라_위치_정면_수평시야각을_주고_없거나_깨졌으면_빈_목록을_준다() throws Exception {
        String version = prefix + "-g1";
        ObjectNode doc = read("calibration.g1_auto.json");
        doc.put("calibration_version", version);
        call("POST", "/api/calibrations", admin(), upload(doc), 201);
        String uploadedHash = jdbc.queryForObject(
                "SELECT calibration_hash FROM calibration_version WHERE calibration_version = ?",
                String.class,
                version);

        try (Tier0 tier0 = tier0()) {
            ObjectNode msg = update(version, null, "CALIBRATED", "INITIAL", base);
            ((ObjectNode) msg.get("payload")).put("calibration_hash", uploadedHash);
            tier0.send(msg);
        }
        awaitCount(() -> messages(version), 1);

        // 권한 확인: 토큰 없으면 401, VIEWER/ADMIN 은 200
        HttpRequest unauth = HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/api/calibrations/current"))
                .GET().build();
        assertThat(http.send(unauth, HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(401);

        JsonNode current = call("/api/calibrations/current", viewer(), 200);
        JsonNode asAdmin = call("/api/calibrations/current", admin(), 200);
        assertThat(asAdmin).isEqualTo(current);
        assertThat(schemas.violations("messages/calibration_status", MAPPER.writeValueAsString(current))).isEmpty();
        assertThat(current.get("calibration_version").asString()).isEqualTo(version);
        assertThat(current.get("document_uploaded").asBoolean()).isTrue();

        JsonNode poses = current.get("camera_poses");
        assertThat(poses).hasSize(4);
        JsonNode cam1 = poses.get(0);
        assertThat(cam1.get("camera_id").asString()).isEqualTo("cam1");
        // C = -R^T t -> (0.20, 0.20, 2.50) m
        assertThat(cam1.get("position_xyz").get(0).asDouble()).isCloseTo(0.2001, Offset.offset(1e-3));
        assertThat(cam1.get("position_xyz").get(1).asDouble()).isCloseTo(0.2001, Offset.offset(1e-3));
        assertThat(cam1.get("position_xyz").get(2).asDouble()).isCloseTo(2.4998, Offset.offset(1e-3));
        // 정면 단위 벡터 R[2]
        double fx = cam1.get("forward_xyz").get(0).asDouble();
        double fy = cam1.get("forward_xyz").get(1).asDouble();
        double fz = cam1.get("forward_xyz").get(2).asDouble();
        assertThat(fx).isCloseTo(0.7574, Offset.offset(1e-3));
        assertThat(fy).isCloseTo(0.5581, Offset.offset(1e-3));
        assertThat(fz).isCloseTo(-0.3388, Offset.offset(1e-3));
        assertThat(Math.sqrt(fx * fx + fy * fy + fz * fz)).isCloseTo(1.0, Offset.offset(1e-6));
        // hfov_rad = 2 * atan(1920 / (2 * 1010.0))
        assertThat(cam1.get("hfov_rad").asDouble())
                .isCloseTo(2.0 * Math.atan(1920.0 / (2.0 * 1010.0)), Offset.offset(1e-6));
        // 원문 R, t, K, distortion 은 노출하지 않는다
        assertThat(cam1.has("R")).isFalse();
        assertThat(cam1.has("t")).isFalse();
        assertThat(cam1.has("K")).isFalse();
        assertThat(cam1.has("distortion")).isFalse();

        JsonNode cam2 = poses.get(1);
        assertThat(cam2.get("camera_id").asString()).isEqualTo("cam2");
        assertThat(cam2.get("position_xyz").get(0).asDouble()).isCloseTo(7.7996, Offset.offset(1e-3));
        assertThat(cam2.get("position_xyz").get(1).asDouble()).isCloseTo(0.2004, Offset.offset(1e-3));
        assertThat(cam2.get("position_xyz").get(2).asDouble()).isCloseTo(2.4994, Offset.offset(1e-3));
        assertThat(cam2.get("hfov_rad").asDouble())
                .isCloseTo(2.0 * Math.atan(1920.0 / (2.0 * 1010.0)), Offset.offset(1e-6));

        // 저장된 문서의 회전 행렬이 비직교이거나 JSON 이 깨졌으면 지어내지 않고 빈 배열을 준다
        ObjectNode badRotation = doc.deepCopy();
        ((ObjectNode) badRotation.get("cameras").get(0)).set("R",
                MAPPER.valueToTree(new int[][] {{1, 0, 0}, {0, 1, 0}, {0, 0, 0}}));
        jdbc.update("UPDATE calibration_document SET content = ? WHERE calibration_version = ?",
                MAPPER.writeValueAsBytes(badRotation), version);
        JsonNode afterBadR = call("/api/calibrations/current", viewer(), 200);
        assertThat(afterBadR.get("camera_poses")).isEmpty();

        jdbc.update("UPDATE calibration_document SET content = ? WHERE calibration_version = ?",
                "{broken-json".getBytes(StandardCharsets.UTF_8), version);
        JsonNode afterCorrupt = call("/api/calibrations/current", viewer(), 200);
        assertThat(afterCorrupt.get("camera_poses")).isEmpty();
    }

    // ---- 도우미

    /** 계약 예시 calibration_update 를 이 시험의 버전·Tier 0·시각으로 바꾼다. effective_at 은 sent_at + 600 */
    private ObjectNode update(String version, String previous, String state, String reason, long sentAt) throws Exception {
        ObjectNode root = read("calibration_update.json");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", sentAt);
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("calibration_version", version);
        p.put("previous_calibration_version", previous);
        p.put("calibration_state", state);
        p.put("reason", reason);
        p.put("effective_at", sentAt + 600);
        p.put("calibration_hash", EXAMPLE_HASH);
        return root;
    }

    /** 계약 예시 보정 문서. claimedHash 가 있으면 문서의 content_hash 칸에 적는다(EC2 는 믿지 않고 다시 잰다) */
    private ObjectNode document(String version, String claimedHash) throws Exception {
        ObjectNode doc = read("calibration.json");
        doc.put("calibration_version", version);
        if (claimedHash != null) {
            doc.put("content_hash", claimedHash);
        }
        return doc;
    }

    private String previous(String version) {
        return jdbc.queryForObject("SELECT previous_calibration_version FROM calibration_version WHERE calibration_version = ?",
                String.class, version);
    }

    private static ObjectNode upload(ObjectNode doc) {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("reason", "시험 보정");
        body.set("document", doc);
        return body;
    }

    private void guide(String version, String cameraId, String message) {
        jdbc.update("""
                INSERT INTO coverage_guide (calibration_version, camera_id, message, metrics, created_at)
                VALUES (?, ?, ?, '{"floor_coverage_ratio": 0.42}'::jsonb, now())
                """, version, cameraId, message);
    }

    private static ObjectNode read(String name) throws Exception {
        return (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples").resolve(name)));
    }

    private int messages(String version) {
        return jdbc.queryForObject("SELECT count(*) FROM calibration_update_message WHERE calibration_version = ?",
                Integer.class, version);
    }

    /** 지표는 앱 전체에서 누적된다. 시험은 보내기 전후 차이만 본다 */
    private double counter(String result) {
        Counter counter = meters.find(METRIC).tags("msg_type", "calibration_update", "result", result).counter();
        return counter == null ? 0 : counter.count();
    }

    private static void awaitCount(Supplier<Integer> actual, int expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (actual.get() < expected && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(actual.get()).isEqualTo(expected);
    }

    private JsonNode call(String path, String token, int expectedStatus) throws Exception {
        return call("GET", path, token, null, expectedStatus);
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

    private String viewer() {
        return token("VIEWER");
    }

    private String token(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(adminId)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private Tier0 tier0() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TIER0_TOKEN);
        WebSocketSession session = new StandardWebSocketClient().execute(new AbstractWebSocketHandler() { }, headers,
                URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS);
        return new Tier0(session);
    }

    private record Tier0(WebSocketSession session) implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(MAPPER.writeValueAsString(message)));
        }

        @Override
        public void close() throws Exception {
            session.close();
        }
    }
}
