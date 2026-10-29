package com.ssafy.simai.domain.avatar;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.machine.MachineKind;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import io.micrometer.core.instrument.MeterRegistry;
import java.lang.reflect.Type;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.messaging.simp.stomp.StompFrameHandler;
import org.springframework.messaging.simp.stomp.StompHeaders;
import org.springframework.messaging.simp.stomp.StompSession;
import org.springframework.messaging.simp.stomp.StompSessionHandlerAdapter;
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
import org.springframework.web.socket.messaging.WebSocketStompClient;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * S15P31A101-325 통합 시험:
 * <ul>
 * <li>/ws/worker/avatar 워커 토큰 인증(토큰 없음·틀린 토큰·기기 토큰·폐기 토큰 거부)</li>
 * <li>avatar_pose_batch·avatar_asset 계약 스키마 및 스키마 밖 규칙·512 KiB 한도 검증</li>
 * <li>msg_id 중복 제거 및 사람별 (identity_revision, capture_time_ms) 최신 규칙, 자산 차수 비교</li>
 * <li>/topic/avatar 로 avatar_frame(1.0.0) 발행 및 사람 제거(world_state 소멸·30초 만료)</li>
 * <li>GET /api/avatars/{worldId}/epochs/{streamEpoch}/versions/{appearanceVersion}/{file} 관제 앱 JWT 인증 및 302 서명 주소</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class AvatarRelayIntegrationTest {

    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final String TIER0_TOKEN = "test-only-tier0-token";

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    MachineTokenService machineTokens;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    AvatarStreamService avatarStream;

    @Autowired
    MeterRegistry meters;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private String workerId;
    private String workerToken;
    private String deviceId;
    private String deviceToken;

    @BeforeEach
    void setUp() {
        avatarStream.clearState();
        jdbc.update("DELETE FROM avatar_asset");
        workerId = "worker-" + tag;
        workerToken = machineTokens.registerWorker(workerId, List.of("tbm_report")).token();
        deviceId = "laptop-" + tag;
        deviceToken = machineTokens.registerDevice(deviceId, "LAPTOP").token();
    }

    @AfterEach
    void tearDown() {
        avatarStream.clearState();
        jdbc.update("DELETE FROM avatar_asset");
        jdbc.update("DELETE FROM upload_grant WHERE requested_by_worker = ? OR requested_by_device = ?", workerId, deviceId);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", workerId);
        jdbc.update("DELETE FROM device WHERE device_id = ?", deviceId);
    }

    @Test
    void 워커_토큰이_없거나_틀리거나_기기_토큰이거나_폐기되면_아바타_WebSocket_연결을_거부한다() throws Exception {
        assertThatThrownBy(() -> connectWorkerWs(null)).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> connectWorkerWs("wrong-token")).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> connectWorkerWs(TIER0_TOKEN)).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> connectWorkerWs(deviceToken)).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> connectWorkerWs(jwt("VIEWER"))).isInstanceOf(ExecutionException.class);

        try (WorkerWs ws = connectWorkerWs(workerToken)) {
            assertThat(ws.session().isOpen()).isTrue();
        }

        String revokedId = "revoked-" + tag;
        String revokedToken = machineTokens.registerWorker(revokedId, List.of("tbm_report")).token();
        machineTokens.revoke(MachineKind.WORKER, revokedId);
        try {
            assertThatThrownBy(() -> connectWorkerWs(revokedToken)).isInstanceOf(ExecutionException.class);
        } finally {
            jdbc.update("DELETE FROM worker WHERE worker_id = ?", revokedId);
        }
    }

    @Test
    void 정상_자산과_포즈_묶음을_받으면_계약에_맞는_avatar_frame_을_발행하고_포즈는_DB에_저장하지_않는다() throws Exception {
        try (Viewer viewer = stomp(jwt("VIEWER")); WorkerWs worker = connectWorkerWs(workerToken)) {
            viewer.subscribe(RealtimeTopics.AVATAR);

            ObjectNode assetUntextured = example("avatar_asset.json");
            ObjectNode assetTextured = example("avatar_asset.textured.json");
            worker.send(assetUntextured);
            worker.send(assetTextured);
            awaitAssetRows(2);

            // 같은 (world_id, stream_epoch, appearance_version) 재알림은 새 msg_id 여도 한 번만 기록한다
            ObjectNode reannounce = assetTextured.deepCopy();
            reannounce.put("msg_id", UUID.randomUUID().toString());
            worker.send(reannounce);
            Thread.sleep(100);
            assertThat(jdbc.queryForObject("SELECT count(*) FROM avatar_asset", Integer.class)).isEqualTo(2);

            ObjectNode batch = example("avatar_pose_batch.json");
            JsonNode frame = viewer.awaitAvatarAfterSending(worker, batch);

            assertThat(schemas.violations("messages/avatar_frame", mapper.writeValueAsString(frame))).isEmpty();
            assertThat(frame.get("source_id").stringValue()).isEqualTo("ec2");
            assertThat(frame.get("msg_type").stringValue()).isEqualTo("avatar_frame");
            assertThat(frame.get("schema_version").stringValue()).isEqualTo("1.0.0");

            JsonNode avatars = frame.at("/payload/avatars");
            assertThat(avatars).hasSize(2);

            JsonNode w12 = avatars.get(0);
            assertThat(w12.get("world_id").stringValue()).isEqualTo("w-0012");
            assertThat(w12.get("pose")).isEqualTo(batch.at("/payload/poses/0/pose"));
            assertThat(w12.at("/asset/mesh_url").stringValue())
                    .isEqualTo("/api/avatars/w-0012/epochs/1760342000000/versions/3/mesh.glb");
            assertThat(w12.at("/asset/texture_url").stringValue())
                    .isEqualTo("/api/avatars/w-0012/epochs/1760342000000/versions/3/albedo.png");
            assertThat(w12.at("/asset/surface_source_url").stringValue())
                    .isEqualTo("/api/avatars/w-0012/epochs/1760342000000/versions/3/surface_source.png");

            JsonNode w13 = avatars.get(1);
            assertThat(w13.get("world_id").stringValue()).isEqualTo("w-0013");
            assertThat(w13.get("pose")).isEqualTo(batch.at("/payload/poses/1/pose"));
            assertThat(w13.at("/asset/mesh_url").stringValue())
                    .isEqualTo("/api/avatars/w-0013/epochs/1760342000000/versions/0/mesh.glb");
            assertThat(w13.at("/asset/texture_url").isNull()).isTrue();
            assertThat(w13.at("/asset/surface_source_url").isNull()).isTrue();

            // 포즈는 DB 에 저장하지 않으며 Tier 0 트랙·골격 표를 건드리지 않는다(규칙 1)
            assertThat(jdbc.queryForObject(
                    "SELECT count(*) FROM world_track_state WHERE world_id IN ('w-0012', 'w-0013')",
                    Integer.class)).isZero();
            assertThat(jdbc.queryForObject(
                    "SELECT count(*) FROM skeleton_3d WHERE world_id IN ('w-0012', 'w-0013')",
                    Integer.class)).isZero();
        }
    }

    @Test
    void 스키마_위반_스키마_밖_규칙_위반_512KiB_초과_메시지는_버리고_연결은_유지한다() throws Exception {
        try (Viewer viewer = stomp(jwt("VIEWER")); WorkerWs worker = connectWorkerWs(workerToken)) {
            viewer.subscribe(RealtimeTopics.AVATAR);
            worker.send(example("avatar_asset.json"));
            worker.send(example("avatar_asset.textured.json"));
            awaitAssetRows(2);

            double invalidPosesBefore = counter(AvatarStreamService.AVATAR_POSE_BATCH, "invalid");
            double invalidAssetsBefore = counter(AvatarStreamService.AVATAR_ASSET, "invalid");
            double invalidOtherBefore = counter("other", "invalid");

            // 1. 스키마 위반: 위치 필드(pelvis_xyz) 포함(AGENTS 규칙 1 위반)
            ObjectNode withPosition = example("avatar_pose_batch.json");
            ((ObjectNode) withPosition.at("/payload/poses/0")).putArray("pelvis_xyz").add(1.0).add(2.0).add(0.9);
            worker.send(withPosition);

            // 2. 스키마 밖 규칙 위반: 회전각 길이 > π ([3.0, 3.0, 0.0])
            ObjectNode rotTooLong = example("avatar_pose_batch.json");
            ((ArrayNode) rotTooLong.at("/payload/poses/0/pose/joint_rotations"))
                    .set(1, mapper.createArrayNode().add(3.0).add(3.0).add(0.0));
            worker.send(rotTooLong);

            // 3. 스키마 밖 규칙 위반: 관절 0 회전이 0 이 아님
            ObjectNode nonZeroJoint0 = example("avatar_pose_batch.json");
            ((ArrayNode) nonZeroJoint0.at("/payload/poses/0/pose/joint_rotations"))
                    .set(0, mapper.createArrayNode().add(0.0).add(0.0).add(0.5));
            worker.send(nonZeroJoint0);

            // 4. 스키마 밖 규칙 위반: joint_rotations 길이 != 묶인 자산의 joint_count(127)
            ObjectNode wrongJointCount = example("avatar_pose_batch.json");
            ((ArrayNode) wrongJointCount.at("/payload/poses/0/pose/joint_rotations")).remove(126);
            worker.send(wrongJointCount);

            // 5. 스키마 밖 규칙 위반: 한 묶음에 같은 world_id 중복
            ObjectNode duplicatePerson = example("avatar_pose_batch.json");
            ArrayNode dupPoses = (ArrayNode) duplicatePerson.at("/payload/poses");
            dupPoses.add(dupPoses.get(0).deepCopy());
            worker.send(duplicatePerson);

            // 6. 자산 스키마 밖 규칙 위반: 표면 비율 합 != 1, 다른 자산 파일 주소 공유(덮어쓰기)
            ObjectNode badRatioAsset = example("avatar_asset.textured.json");
            ((ObjectNode) badRatioAsset.get("payload")).put("appearance_version", 4);
            ((ObjectNode) badRatioAsset.get("payload")).put("previous_appearance_version", 3);
            ((ObjectNode) badRatioAsset.at("/payload/surface_source/ratios")).put("UNOBSERVED", 0.10);
            worker.send(badRatioAsset);

            ObjectNode sharedFileAsset = example("avatar_asset.json");
            ((ObjectNode) sharedFileAsset.get("payload")).put("stream_epoch", 1760342060000L);
            // mesh.object_uri 는 이전 epoch 자산과 그대로 같아서 파일 덮어쓰기 위반
            worker.send(sharedFileAsset);

            // 7. 512 KiB 초과 메시지: 버리고 연결은 끊지 않는다
            String oversize = "{\"schema_version\":\"1.0.0\",\"msg_type\":\"avatar_pose_batch\",\"pad\":\""
                    + "x".repeat(AvatarStreamService.MAX_MESSAGE_BYTES + 1024) + "\"}";
            worker.sendRaw(oversize);

            awaitCounterAtLeast(AvatarStreamService.AVATAR_POSE_BATCH, "invalid", invalidPosesBefore + 5);
            awaitCounterAtLeast(AvatarStreamService.AVATAR_ASSET, "invalid", invalidAssetsBefore + 2);
            awaitCounterAtLeast("other", "invalid", invalidOtherBefore + 1);
            assertThat(viewer.poll(200)).isNull();

            // 같은 WebSocket 세션이 여전히 살아 있어 정상 묶음을 보내면 바로 /topic/avatar 로 발행된다
            assertThat(worker.session().isOpen()).isTrue();
            ObjectNode validBatch = example("avatar_pose_batch.json");
            JsonNode frame = viewer.awaitAvatarAfterSending(worker, validBatch);
            assertThat(frame.at("/payload/avatars")).hasSize(2);
        }
    }

    @Test
    void 중복_msg_id_와_과거_시각_포즈는_버리고_자산_차수가_포즈보다_크면_asset_을_비운다() throws Exception {
        try (Viewer viewer = stomp(jwt("VIEWER")); WorkerWs worker = connectWorkerWs(workerToken)) {
            viewer.subscribe(RealtimeTopics.AVATAR);
            worker.send(example("avatar_asset.json"));
            worker.send(example("avatar_asset.textured.json"));
            awaitAssetRows(2);

            ObjectNode firstBatch = example("avatar_pose_batch.json");
            JsonNode firstFrame = viewer.awaitAvatarAfterSending(worker, firstBatch);
            UUID firstMsgId = UUID.fromString(firstBatch.get("msg_id").stringValue());
            assertThat(firstFrame.at("/payload/avatars")).hasSize(2);

            double dupBefore = counter(AvatarStreamService.AVATAR_POSE_BATCH, "duplicate");

            // 1. 같은 msg_id 재전송(촬영 시각이 더 커도 msg_id 중복이라 버림)
            ObjectNode sameMsgId = example("avatar_pose_batch.json");
            sameMsgId.put("msg_id", firstMsgId.toString());
            ((ObjectNode) sameMsgId.at("/payload/poses/0")).put("capture_time_ms", 1760342409999L);
            worker.send(sameMsgId);

            // 2. 새 msg_id 이지만 촬영 시각이 같거나 과거인 묶음은 모두 버림
            ObjectNode olderTime = example("avatar_pose_batch.json");
            ((ObjectNode) olderTime.at("/payload/poses/0")).put("capture_time_ms", 1760342400000L);
            ((ObjectNode) olderTime.at("/payload/poses/1")).put("capture_time_ms", 1760342400100L);
            worker.send(olderTime);

            awaitCounterAtLeast(AvatarStreamService.AVATAR_POSE_BATCH, "duplicate", dupBefore + 2);
            assertThat(viewer.poll(200)).isNull();

            // 3. w-0012 는 과거 시각이고 w-0013 만 새 촬영 시각이면 갱신된 w-0013 만 avatar_frame 에 실린다
            ObjectNode partialUpdate = example("avatar_pose_batch.json");
            ((ObjectNode) partialUpdate.at("/payload/poses/0")).put("capture_time_ms", 1760342400050L);
            ((ObjectNode) partialUpdate.at("/payload/poses/1")).put("capture_time_ms", 1760342400500L);
            worker.send(partialUpdate);
            JsonNode partialFrame = viewer.await();
            assertThat(partialFrame.at("/payload/avatars")).hasSize(1);
            assertThat(partialFrame.at("/payload/avatars/0/world_id").stringValue()).isEqualTo("w-0013");
            assertThat(partialFrame.at("/payload/avatars/0/capture_time_ms").longValue()).isEqualTo(1760342400500L);

            // 4. 자산 identity_revision(5) > 포즈 identity_revision(3) 이면 asset 은 null 로 싣는다
            ObjectNode futureIdentityAsset = example("avatar_asset.textured.json");
            ObjectNode faPayload = (ObjectNode) futureIdentityAsset.get("payload");
            faPayload.put("identity_revision", 5);
            faPayload.put("appearance_version", 4);
            faPayload.put("previous_appearance_version", 3);
            ((ObjectNode) faPayload.get("mesh"))
                    .put("object_uri", "s3://simai-avatars/w-0012/e1760342000000/v4/mesh.glb");
            ((ObjectNode) faPayload.get("texture"))
                    .put("object_uri", "s3://simai-avatars/w-0012/e1760342000000/v4/albedo.png");
            ((ObjectNode) faPayload.get("surface_source"))
                    .put("mask_uri", "s3://simai-avatars/w-0012/e1760342000000/v4/surface_source.png");
            worker.send(futureIdentityAsset);
            awaitAssetRows(3);

            ObjectNode poseWithOlderIdentity = example("avatar_pose_batch.json");
            ArrayNode poses = (ArrayNode) poseWithOlderIdentity.at("/payload/poses");
            poses.remove(1);
            ObjectNode p0 = (ObjectNode) poses.get(0);
            p0.put("identity_revision", 3);
            p0.put("appearance_version", 4);
            p0.put("capture_time_ms", 1760342401000L);
            worker.send(poseWithOlderIdentity);

            JsonNode assetNullFrame = viewer.await();
            assertThat(schemas.violations("messages/avatar_frame", mapper.writeValueAsString(assetNullFrame))).isEmpty();
            assertThat(assetNullFrame.at("/payload/avatars/0/world_id").stringValue()).isEqualTo("w-0012");
            assertThat(assetNullFrame.at("/payload/avatars/0/asset").isNull()).isTrue();
        }
    }

    @Test
    void world_state_에서_사라지거나_30초가_지난_사람의_최신_포즈는_지운다() throws Exception {
        try (WorkerWs worker = connectWorkerWs(workerToken)) {
            worker.send(example("avatar_asset.json"));
            worker.send(example("avatar_asset.textured.json"));
            awaitAssetRows(2);

            worker.send(example("avatar_pose_batch.json"));
            awaitPosePresent("w-0012");
            awaitPosePresent("w-0013");

            // world_state 에 w-0012 만 남고 w-0013 이 빠지면 w-0013 의 최신 포즈를 지운다
            ObjectNode ws = example("world_state.json");
            ArrayNode tracks = ((ObjectNode) ws.get("payload")).putArray("tracks");
            tracks.addObject().put("world_id", "w-0012");
            avatarStream.onWorldState(new WorldStateStored(I1Envelope.parse(ws)));

            assertThat(avatarStream.latestPose("w-0012")).isPresent();
            assertThat(avatarStream.latestPose("w-0013")).isEmpty();

            // 30초 넘게 새 포즈가 오지 않으면 남은 사람도 지운다
            avatarStream.evictExpiredPoses(Instant.now().plusSeconds(31));
            assertThat(avatarStream.latestPose("w-0012")).isEmpty();
        }
    }

    @Test
    void 뷰어_아바타_자산_조회는_JWT_인증을_요구하고_서명된_S3_주소로_302_리다이렉트한다() throws Exception {
        try (WorkerWs worker = connectWorkerWs(workerToken)) {
            worker.send(example("avatar_asset.json"));
            worker.send(example("avatar_asset.textured.json"));
            awaitAssetRows(2);
        }

        HttpClient http = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NEVER).build();
        String meshPath = "/api/avatars/w-0012/epochs/1760342000000/versions/3/mesh.glb";
        String texturePath = "/api/avatars/w-0012/epochs/1760342000000/versions/3/albedo.png";
        String maskPath = "/api/avatars/w-0012/epochs/1760342000000/versions/3/surface_source.png";
        String untexturedAlbedoPath = "/api/avatars/w-0013/epochs/1760342000000/versions/0/albedo.png";

        // 토큰 없음·워커 토큰·틀린 토큰은 401
        assertThat(get(http, meshPath, null).statusCode()).isEqualTo(401);
        assertThat(get(http, meshPath, workerToken).statusCode()).isEqualTo(401);
        assertThat(get(http, meshPath, "invalid-jwt").statusCode()).isEqualTo(401);

        // VIEWER JWT 로 메시·텍스처·표면 출처 지도 조회 시 302 서명 주소 리다이렉트
        String viewerJwt = jwt("VIEWER");
        HttpResponse<String> meshRes = get(http, meshPath, viewerJwt);
        assertThat(meshRes.statusCode()).isEqualTo(302);
        assertThat(meshRes.headers().firstValue(HttpHeaders.LOCATION)).hasValueSatisfying(loc -> assertThat(loc)
                .startsWith("http://127.0.0.1:9000/simai-test-bucket/w-0012/e1760342000000/v3/mesh.glb?")
                .contains("X-Amz-Signature="));

        HttpResponse<String> texRes = get(http, texturePath, viewerJwt);
        assertThat(texRes.statusCode()).isEqualTo(302);
        assertThat(texRes.headers().firstValue(HttpHeaders.LOCATION)).hasValueSatisfying(loc -> assertThat(loc)
                .startsWith("http://127.0.0.1:9000/simai-test-bucket/w-0012/e1760342000000/v3/albedo.png?")
                .contains("X-Amz-Signature="));

        HttpResponse<String> maskRes = get(http, maskPath, viewerJwt);
        assertThat(maskRes.statusCode()).isEqualTo(302);
        assertThat(maskRes.headers().firstValue(HttpHeaders.LOCATION)).hasValueSatisfying(loc -> assertThat(loc)
                .startsWith("http://127.0.0.1:9000/simai-test-bucket/w-0012/e1760342000000/v3/surface_source.png?")
                .contains("X-Amz-Signature="));

        // 텍스처가 null 인 자산의 albedo.png 나 없는 버전·파일은 404
        assertThat(get(http, untexturedAlbedoPath, viewerJwt).statusCode()).isEqualTo(404);
        assertThat(get(http, "/api/avatars/w-0012/epochs/1760342000000/versions/99/mesh.glb", viewerJwt).statusCode())
                .isEqualTo(404);
        assertThat(get(http, "/api/avatars/w-0012/epochs/1760342000000/versions/3/other.bin", viewerJwt).statusCode())
                .isEqualTo(404);
    }

    private HttpResponse<String> get(HttpClient http, String path, String bearerToken) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path)).GET();
        if (bearerToken != null) {
            builder.header(HttpHeaders.AUTHORIZATION, "Bearer " + bearerToken);
        }
        return http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    private void awaitAssetRows(int expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        while (System.nanoTime() < deadline) {
            Integer count = jdbc.queryForObject("SELECT count(*) FROM avatar_asset", Integer.class);
            if (count != null && count >= expected) {
                return;
            }
            Thread.sleep(30);
        }
        assertThat(jdbc.queryForObject("SELECT count(*) FROM avatar_asset", Integer.class)).isEqualTo(expected);
    }

    private void awaitPosePresent(String worldId) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        while (System.nanoTime() < deadline) {
            if (avatarStream.latestPose(worldId).isPresent()) {
                return;
            }
            Thread.sleep(30);
        }
        assertThat(avatarStream.latestPose(worldId)).isPresent();
    }

    private double counter(String msgType, String result) {
        return meters.counter(AvatarStreamService.METRIC, "msg_type", msgType, "result", result).count();
    }

    private void awaitCounterAtLeast(String msgType, String result, double expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        while (System.nanoTime() < deadline) {
            if (counter(msgType, result) >= expected) {
                return;
            }
            Thread.sleep(30);
        }
        assertThat(counter(msgType, result)).isGreaterThanOrEqualTo(expected);
    }

    private ObjectNode example(String name) throws Exception {
        ObjectNode root = (ObjectNode) mapper.readTree(Files.readString(CONTRACTS.resolve("examples").resolve(name)));
        root.put("msg_id", UUID.randomUUID().toString());
        return root;
    }

    private String jwt(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer(JwtConfig.ISSUER)
                .subject("1")
                .issuedAt(now)
                .expiresAt(now.plusSeconds(300))
                .claim(AccessTokenIssuer.ROLE_CLAIM, role)
                .build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims))
                .getTokenValue();
    }

    private WorkerWs connectWorkerWs(String token) throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        if (token != null) {
            headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        WebSocketSession session = new StandardWebSocketClient()
                .execute(new TextWebSocketHandler() {}, headers, URI.create("ws://localhost:" + port + AvatarPaths.WORKER_WS))
                .get(5, TimeUnit.SECONDS);
        return new WorkerWs(session, mapper);
    }

    private Viewer stomp(String token) throws Exception {
        WebSocketStompClient client = new WebSocketStompClient(new StandardWebSocketClient());
        StompHeaders connect = new StompHeaders();
        if (token != null) {
            connect.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        StompSession session = client.connectAsync(
                        URI.create("ws://localhost:" + port + RealtimeTopics.ENDPOINT),
                        new WebSocketHttpHeaders(),
                        connect,
                        new StompSessionHandlerAdapter() {})
                .get(5, TimeUnit.SECONDS);
        return new Viewer(client, session, new LinkedBlockingQueue<>(), mapper);
    }

    private record WorkerWs(WebSocketSession session, ObjectMapper mapper) implements AutoCloseable {

        void send(JsonNode node) throws Exception {
            session.sendMessage(new TextMessage(mapper.writeValueAsString(node)));
        }

        void sendRaw(String raw) throws Exception {
            session.sendMessage(new TextMessage(raw));
        }

        @Override
        public void close() throws Exception {
            if (session.isOpen()) {
                session.close();
            }
        }
    }

    private record Viewer(
            WebSocketStompClient client,
            StompSession session,
            BlockingQueue<JsonNode> queue,
            ObjectMapper mapper) implements AutoCloseable {

        void subscribe(String topic) {
            session.subscribe(topic, new StompFrameHandler() {
                @Override
                public Type getPayloadType(StompHeaders headers) {
                    return byte[].class;
                }

                @Override
                public void handleFrame(StompHeaders headers, Object payload) {
                    queue.add(mapper.readTree(new String((byte[]) payload, StandardCharsets.UTF_8)));
                }
            });
        }

        JsonNode await() throws InterruptedException {
            JsonNode msg = queue.poll(5, TimeUnit.SECONDS);
            assertThat(msg).as("/topic/avatar 메시지를 받아야 한다").isNotNull();
            return msg;
        }

        JsonNode poll(long timeoutMs) throws InterruptedException {
            return queue.poll(timeoutMs, TimeUnit.MILLISECONDS);
        }

        JsonNode awaitAvatarAfterSending(WorkerWs worker, ObjectNode batch) throws Exception {
            for (int attempt = 0; attempt < 20; attempt++) {
                if (attempt > 0) {
                    batch.put("msg_id", UUID.randomUUID().toString());
                    for (JsonNode pose : batch.at("/payload/poses")) {
                        long prev = pose.get("capture_time_ms").longValue();
                        ((ObjectNode) pose).put("capture_time_ms", prev + 1);
                    }
                }
                worker.send(batch);
                JsonNode received = queue.poll(300, TimeUnit.MILLISECONDS);
                if (received != null) {
                    queue.clear();
                    return received;
                }
            }
            throw new AssertionError("/topic/avatar 메시지가 오지 않았다");
        }

        @Override
        public void close() {
            if (session.isConnected()) {
                session.disconnect();
            }
            client.stop();
        }
    }
}
