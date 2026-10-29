package com.ssafy.simai.domain.avatar;

import com.ssafy.simai.domain.realtime.RealtimePublisher;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import com.ssafy.simai.global.contract.ContractSchemas;
import io.micrometer.core.instrument.MeterRegistry;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Service;
import org.springframework.transaction.event.TransactionalEventListener;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * EXP-02 아바타 스트림(I6: avatar_pose_batch·avatar_asset) 수신·검증 및 /topic/avatar(avatar_frame 1.0.0) 중계(S15P31A101-325).
 * <ul>
 * <li>계약 JSON Schema(ContractSchemas)와 스키마 밖 불변식(회전 길이 ≤ π, 관절 0 = [0,0,0], 관절 수 = 자산 joint_count,
 * pelvis_joint_index &lt; joint_count, previous_appearance_version 순서, 표면·얼굴 비율 합 1, 묶음 내 world_id 유일)을 모두 검사한다.</li>
 * <li>본문 512 KiB 상한, 한 묶음 최대 64명, model_version·rig_source 256자 상한을 지킨다.</li>
 * <li>msg_id 중복과 사람별 (identity_revision, capture_time_ms) 역전·동일 시각은 버린다.</li>
 * <li>포즈는 DB 에 저장하지 않고 메모리에서 사람별 최신 1개만 유지한다. 아바타 포즈·자산은 위험 판정이나 Tier 0 위치에 쓰지 않는다(규칙 1).</li>
 * <li>/topic/world(world_state)에서 사라진 world_id 와 30초 넘게 새 포즈가 없는 사람은 메모리에서 지운다.</li>
 * <li>받은 묶음마다 갱신된 사람만 모아 avatar_frame(1.0.0)을 /topic/avatar 로 발행하고, 브로커 송신이 밀리면 오래된 것을 대체한다.</li>
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class AvatarStreamService implements AutoCloseable {

    public static final int MAX_MESSAGE_BYTES = 512 * 1024;
    public static final int MAX_PEOPLE_PER_MESSAGE = 64;
    public static final int MAX_STRING_LENGTH = 256;
    public static final Duration POSE_TTL = Duration.ofSeconds(30);

    static final String METRIC = "simai.avatar.messages";
    static final String AVATAR_POSE_BATCH = "avatar_pose_batch";
    static final String AVATAR_ASSET = "avatar_asset";
    static final String AVATAR_FRAME = "avatar_frame";
    static final String AVATAR_FRAME_VERSION = "1.0.0";
    private static final String OTHER = "other";

    private static final double PI_TOL = 1e-4;
    private static final double RATIO_TOL = 1e-6;
    private static final int MAX_SEEN_MSG_IDS = 10_000;

    private static final Logger log = LoggerFactory.getLogger(AvatarStreamService.class);

    private final ContractSchemas schemas;
    private final AvatarAssetRepository assets;
    private final RealtimePublisher publisher;
    private final ObjectMapper mapper;
    private final MeterRegistry meters;
    private final Clock clock;

    private final Object poseLock = new Object();
    private final Map<String, PersonPoseState> latestPoses = new ConcurrentHashMap<>();
    private final Set<UUID> seenMsgIds = Collections.synchronizedSet(
            Collections.newSetFromMap(new LinkedHashMap<>(1024, 0.75f, true) {
                @Override
                protected boolean removeEldestEntry(Map.Entry<UUID, Boolean> eldest) {
                    return size() > MAX_SEEN_MSG_IDS;
                }
            }));

    private final ExecutorService avatarExecutor =
            Executors.newSingleThreadExecutor(r -> new Thread(r, "stomp-avatar"));
    private final ConcurrentHashMap<String, ObjectNode> pendingFrames = new ConcurrentHashMap<>();

    public AvatarStreamService(
            ContractSchemas schemas,
            AvatarAssetRepository assets,
            RealtimePublisher publisher,
            ObjectMapper mapper,
            MeterRegistry meters,
            Clock clock) {
        this.schemas = schemas;
        this.assets = assets;
        this.publisher = publisher;
        this.mapper = mapper;
        this.meters = meters;
        this.clock = clock;
    }

    /** WebSocket 텍스트 메시지 한 개를 검증·처리한다. 예외로 세션을 닫지 않는다. */
    public void handleRawMessage(String raw, String authenticatedWorkerId) {
        Instant receivedAt = clock.instant();
        if (raw == null || raw.getBytes(StandardCharsets.UTF_8).length > MAX_MESSAGE_BYTES) {
            count(null, "invalid");
            log.warn("아바타 스트림 본문이 비었거나 512 KiB 한도를 넘어 버림: worker={}", authenticatedWorkerId);
            return;
        }

        I1Envelope envelope;
        try {
            JsonNode root = mapper.readTree(raw);
            envelope = I1Envelope.parse(root);
        } catch (RuntimeException e) {
            count(null, "invalid");
            log.warn("아바타 스트림 봉투를 읽지 못해 버림: worker={} reason={}", authenticatedWorkerId, reason(e));
            return;
        }

        String msgType = envelope.msgType();
        if (!AVATAR_POSE_BATCH.equals(msgType) && !AVATAR_ASSET.equals(msgType)) {
            count(null, "unknown_type");
            log.debug("아바타 스트림 지원하지 않는 msg_type 버림: msg_type={} msg_id={}", msgType, envelope.msgId());
            return;
        }
        if (envelope.major() != 1) {
            count(msgType, "unsupported_version");
            log.warn("아바타 스트림 메이저 버전을 읽을 수 없어 버림: msg_type={} schema_version={} msg_id={}",
                    msgType, envelope.schemaVersion(), envelope.msgId());
            return;
        }

        try {
            String contractJson = mapper.writeValueAsString(envelope.toContractJson(mapper));
            List<String> violations = schemas.violations("messages/" + msgType, contractJson);
            if (!violations.isEmpty()) {
                throw new InvalidI1Message("계약 스키마 위반");
            }
            if (AVATAR_ASSET.equals(msgType)) {
                ingestAsset(envelope, authenticatedWorkerId, receivedAt);
            } else {
                ingestPoseBatch(envelope, receivedAt);
            }
        } catch (InvalidI1Message | JacksonException e) {
            count(msgType, "invalid");
            log.warn("아바타 스트림 계약 위반이라 버림: msg_type={} msg_id={} reason={}",
                    msgType, envelope.msgId(), reason(e));
        } catch (RuntimeException e) {
            count(msgType, "failed");
            log.warn("아바타 스트림 처리 실패: msg_type={} msg_id={} error={}",
                    msgType, envelope.msgId(), e.getClass().getSimpleName());
        }
    }

    private void ingestAsset(I1Envelope envelope, String authenticatedWorkerId, Instant receivedAt) {
        AvatarAssetRecord asset = parseAndValidateAsset(envelope, authenticatedWorkerId, receivedAt);
        if (seenMsgIds.contains(envelope.msgId())) {
            count(AVATAR_ASSET, "duplicate");
            return;
        }
        AvatarAssetRepository.RecordResult result = assets.record(asset);
        seenMsgIds.add(envelope.msgId());
        count(AVATAR_ASSET, result == AvatarAssetRepository.RecordResult.STORED ? "stored" : "duplicate");
    }

    private void ingestPoseBatch(I1Envelope envelope, Instant receivedAt) {
        List<PersonPoseState> incoming = parseAndValidatePoseBatch(envelope, receivedAt);
        if (!seenMsgIds.add(envelope.msgId())) {
            count(AVATAR_POSE_BATCH, "duplicate");
            return;
        }

        List<PersonPoseState> updated = new ArrayList<>(incoming.size());
        synchronized (poseLock) {
            evictExpiredLocked(receivedAt);
            for (PersonPoseState candidate : incoming) {
                PersonPoseState current = latestPoses.get(candidate.worldId());
                if (current == null
                        || isNewer(
                                candidate.identityRevision(),
                                candidate.captureTimeMs(),
                                current.identityRevision(),
                                current.captureTimeMs())) {
                    latestPoses.put(candidate.worldId(), candidate);
                    updated.add(candidate);
                }
            }
        }

        if (updated.isEmpty()) {
            count(AVATAR_POSE_BATCH, "duplicate");
            return;
        }

        count(AVATAR_POSE_BATCH, "stored");
        JsonNode inputProvenance = envelope.payload().get("input_provenance");
        ObjectNode framePayload = buildAvatarFramePayload(inputProvenance, updated);
        enqueueAvatarFrame(envelope.sourceId(), framePayload);
    }

    private AvatarAssetRecord parseAndValidateAsset(
            I1Envelope envelope, String authenticatedWorkerId, Instant receivedAt) {
        JsonNode p = envelope.payload();
        String worldId = p.get("world_id").stringValue();
        long streamEpoch = p.get("stream_epoch").longValue();
        int identityRevision = p.get("identity_revision").intValue();
        int appearanceVersion = p.get("appearance_version").intValue();
        JsonNode prevNode = p.get("previous_appearance_version");
        Integer previousAppearanceVersion =
                prevNode == null || prevNode.isNull() ? null : prevNode.intValue();

        if (appearanceVersion == 0 && previousAppearanceVersion != null) {
            throw new InvalidI1Message("appearance_version 0 은 previous_appearance_version 이 null 이어야 한다");
        }
        if (appearanceVersion > 0
                && (previousAppearanceVersion == null
                        || previousAppearanceVersion < 0
                        || previousAppearanceVersion >= appearanceVersion)) {
            throw new InvalidI1Message("previous_appearance_version 이 현재 버전 앞이 아니다");
        }

        String calibrationVersion = p.get("calibration_version").stringValue();
        String modelVersion = p.get("model_version").stringValue();
        if (modelVersion.length() > MAX_STRING_LENGTH) {
            throw new InvalidI1Message("model_version 이 256자를 넘는다");
        }

        JsonNode mesh = p.get("mesh");
        String meshFormat = mesh.get("format").stringValue();
        String meshUri = mesh.get("object_uri").stringValue();
        String meshSha256 = mesh.get("sha256").stringValue();
        long meshSizeBytes = mesh.get("size_bytes").longValue();
        int vertexCount = mesh.get("vertex_count").intValue();
        int faceCount = mesh.get("face_count").intValue();

        JsonNode skeleton = p.get("skeleton");
        int jointCount = skeleton.get("joint_count").intValue();
        String jointParentsSha256 = skeleton.get("joint_parents_sha256").stringValue();
        int pelvisJointIndex = skeleton.get("pelvis_joint_index").intValue();
        String rigSource = skeleton.get("rig_source").stringValue();
        if (rigSource.length() > MAX_STRING_LENGTH) {
            throw new InvalidI1Message("rig_source 가 256자를 넘는다");
        }
        if (pelvisJointIndex < 0 || pelvisJointIndex >= jointCount) {
            throw new InvalidI1Message("pelvis_joint_index 가 joint_count 범위 밖이다");
        }

        JsonNode texture = p.get("texture");
        String textureUri = null;
        String textureSha256 = null;
        if (texture != null && !texture.isNull()) {
            textureUri = texture.get("object_uri").stringValue();
            textureSha256 = texture.get("sha256").stringValue();
        }

        JsonNode surfaceSource = p.get("surface_source");
        JsonNode maskNode = surfaceSource.get("mask_uri");
        String surfaceMaskUri = maskNode == null || maskNode.isNull() ? null : maskNode.stringValue();
        JsonNode surfaceRatios = surfaceSource.get("ratios");
        JsonNode faceRatios = surfaceSource.get("face_ratios");
        validateRatioSum(surfaceRatios, "ratios", "OBSERVED", "INTERPOLATED", "UNOBSERVED");
        validateRatioSum(faceRatios, "face_ratios", "OBSERVED", "UNOBSERVED");

        String facePolicy = p.get("face_policy").stringValue();
        Instant createdAt = Instant.ofEpochMilli(p.get("created_at_ms").longValue());
        String workerId = authenticatedWorkerId != null && !authenticatedWorkerId.isBlank()
                ? authenticatedWorkerId
                : envelope.sourceId();

        return new AvatarAssetRecord(
                new AvatarAssetKey(worldId, streamEpoch, appearanceVersion),
                envelope.msgId(),
                workerId,
                identityRevision,
                previousAppearanceVersion,
                calibrationVersion,
                modelVersion,
                meshFormat,
                meshUri,
                meshSha256,
                meshSizeBytes,
                vertexCount,
                faceCount,
                jointCount,
                jointParentsSha256,
                pelvisJointIndex,
                rigSource,
                textureUri,
                textureSha256,
                surfaceMaskUri,
                surfaceRatios,
                faceRatios,
                facePolicy,
                p.get("input_provenance"),
                p,
                createdAt,
                envelope.sentAt(),
                receivedAt);
    }

    private List<PersonPoseState> parseAndValidatePoseBatch(I1Envelope envelope, Instant receivedAt) {
        JsonNode p = envelope.payload();
        long streamEpoch = p.get("stream_epoch").longValue();
        String calibrationVersion = p.get("calibration_version").stringValue();
        String modelVersion = p.get("model_version").stringValue();
        if (modelVersion.length() > MAX_STRING_LENGTH) {
            throw new InvalidI1Message("model_version 이 256자를 넘는다");
        }
        JsonNode posesNode = p.get("poses");
        if (posesNode == null || !posesNode.isArray()
                || posesNode.isEmpty() || posesNode.size() > MAX_PEOPLE_PER_MESSAGE) {
            throw new InvalidI1Message("poses 개수가 범위를 벗어났다");
        }

        Set<String> worldIdsInBatch = new HashSet<>();
        List<PersonPoseState> parsed = new ArrayList<>(posesNode.size());
        long receivedAtMs = receivedAt.toEpochMilli();

        for (JsonNode item : posesNode) {
            String worldId = item.get("world_id").stringValue();
            if (!worldIdsInBatch.add(worldId)) {
                throw new InvalidI1Message("한 묶음 안에 같은 world_id 가 둘 이상 있다");
            }
            int identityRevision = item.get("identity_revision").intValue();
            long captureTimeMs = item.get("capture_time_ms").longValue();
            int appearanceVersion = item.get("appearance_version").intValue();
            String jointSource = item.get("joint_source").stringValue();
            JsonNode pose = item.get("pose");

            AvatarAssetKey assetKey = new AvatarAssetKey(worldId, streamEpoch, appearanceVersion);
            Integer expectedJointCount = assets.find(assetKey).map(AvatarAssetRecord::jointCount).orElse(null);
            validatePoseRotations(pose, expectedJointCount);

            parsed.add(new PersonPoseState(
                    worldId,
                    streamEpoch,
                    identityRevision,
                    captureTimeMs,
                    receivedAtMs,
                    receivedAt,
                    calibrationVersion,
                    appearanceVersion,
                    jointSource,
                    pose));
        }
        return parsed;
    }

    private static void validatePoseRotations(JsonNode pose, Integer expectedJointCount) {
        JsonNode rootRot = pose.get("root_rotation");
        validateAxisAngleLength(rootRot, "root_rotation");

        JsonNode jointRotations = pose.get("joint_rotations");
        if (jointRotations == null || !jointRotations.isArray()) {
            throw new InvalidI1Message("joint_rotations 가 배열이 아니다");
        }
        if (expectedJointCount != null && jointRotations.size() != expectedJointCount) {
            throw new InvalidI1Message("joint_rotations 길이가 자산 joint_count 와 다르다");
        }
        if (!jointRotations.isEmpty()) {
            JsonNode joint0 = jointRotations.get(0);
            if (joint0 != null && !joint0.isNull()) {
                for (JsonNode c : joint0) {
                    if (Math.abs(c.doubleValue()) > PI_TOL) {
                        throw new InvalidI1Message("관절 0 회전이 0 이 아니다");
                    }
                }
            }
        }
        for (JsonNode rot : jointRotations) {
            if (rot != null && !rot.isNull()) {
                validateAxisAngleLength(rot, "joint_rotations");
            }
        }
    }

    private static void validateAxisAngleLength(JsonNode vec, String field) {
        if (vec == null || !vec.isArray() || vec.size() != 3) {
            throw new InvalidI1Message(field + " 가 길이 3 배열이 아니다");
        }
        double x = vec.get(0).doubleValue();
        double y = vec.get(1).doubleValue();
        double z = vec.get(2).doubleValue();
        double norm = Math.hypot(x, Math.hypot(y, z));
        if (!Double.isFinite(norm) || norm > Math.PI + PI_TOL) {
            throw new InvalidI1Message(field + " 회전각 길이가 π 를 넘는다");
        }
    }

    private static void validateRatioSum(JsonNode ratios, String field, String... keys) {
        if (ratios == null || !ratios.isObject()) {
            throw new InvalidI1Message(field + " 가 객체가 아니다");
        }
        double sum = 0.0;
        for (String key : keys) {
            JsonNode v = ratios.get(key);
            if (v == null || !v.isNumber()) {
                throw new InvalidI1Message(field + "." + key + " 가 수가 아니다");
            }
            sum += v.doubleValue();
        }
        if (Math.abs(sum - 1.0) > RATIO_TOL) {
            throw new InvalidI1Message(field + " 합이 1 이 아니다");
        }
    }

    private ObjectNode buildAvatarFramePayload(JsonNode inputProvenance, List<PersonPoseState> updated) {
        ObjectNode payload = mapper.createObjectNode();
        payload.set("input_provenance", inputProvenance);
        ArrayNode avatars = payload.putArray("avatars");
        for (PersonPoseState state : updated) {
            avatars.add(toViewerAvatarNode(state));
        }
        return payload;
    }

    private ObjectNode toViewerAvatarNode(PersonPoseState state) {
        ObjectNode node = mapper.createObjectNode();
        node.put("world_id", state.worldId());
        node.put("identity_revision", state.identityRevision());
        node.put("capture_time_ms", state.captureTimeMs());
        node.put("received_at_ms", state.receivedAtMs());
        node.put("calibration_version", state.calibrationVersion());
        node.put("appearance_version", state.appearanceVersion());
        node.put("joint_source", state.jointSource());
        node.set("pose", state.pose());

        AvatarAssetKey assetKey = new AvatarAssetKey(state.worldId(), state.streamEpoch(), state.appearanceVersion());
        Optional<AvatarAssetRecord> boundAsset = assets.find(assetKey)
                .filter(a -> a.identityRevision() <= state.identityRevision());
        if (boundAsset.isPresent()) {
            AvatarAssetRecord a = boundAsset.get();
            ObjectNode assetNode = mapper.createObjectNode();
            assetNode.put("stream_epoch", a.key().streamEpoch());
            assetNode.put("identity_revision", a.identityRevision());
            assetNode.put("mesh_url", a.meshUrl());
            assetNode.put("mesh_sha256", a.meshSha256());
            assetNode.put("mesh_format", a.meshFormat());
            assetNode.put("joint_count", a.jointCount());
            assetNode.put("joint_parents_sha256", a.jointParentsSha256());
            assetNode.put("pelvis_joint_index", a.pelvisJointIndex());
            if (a.textureUrl() == null) {
                assetNode.putNull("texture_url");
            } else {
                assetNode.put("texture_url", a.textureUrl());
            }
            if (a.surfaceSourceUrl() == null) {
                assetNode.putNull("surface_source_url");
            } else {
                assetNode.put("surface_source_url", a.surfaceSourceUrl());
            }
            assetNode.set("surface_ratios", a.surfaceRatios());
            assetNode.set("face_ratios", a.faceRatios());
            assetNode.put("face_policy", a.facePolicy());
            node.set("asset", assetNode);
        } else {
            node.putNull("asset");
        }
        return node;
    }

    private void enqueueAvatarFrame(String sourceId, ObjectNode framePayload) {
        if (outboundBytes(framePayload) > MAX_MESSAGE_BYTES) {
            count(AVATAR_FRAME, "invalid");
            log.warn("avatar_frame 본문이 512 KiB 를 넘어 버림: source_id={}", sourceId);
            return;
        }
        String key = sourceId == null ? "worker" : sourceId;
        final boolean[] replaced = {false};
        pendingFrames.compute(key, (k, prev) -> {
            if (prev == null) {
                return framePayload;
            }
            replaced[0] = true;
            return coalescePending(prev, framePayload);
        });
        if (replaced[0]) {
            meters.counter("simai.i2.messages", "topic", RealtimeTopics.AVATAR, "result", "replaced").increment();
            return;
        }
        try {
            avatarExecutor.execute(() -> {
                try {
                    ObjectNode latest = pendingFrames.remove(key);
                    if (latest != null) {
                        publisher.publish(RealtimeTopics.AVATAR, AVATAR_FRAME, AVATAR_FRAME_VERSION, latest);
                    }
                } catch (RuntimeException e) {
                    meters.counter("simai.i2.messages", "topic", RealtimeTopics.AVATAR, "result", "failed").increment();
                    log.warn("STOMP /topic/avatar 발행 실패", e);
                }
            });
        } catch (RejectedExecutionException e) {
            meters.counter("simai.i2.messages", "topic", RealtimeTopics.AVATAR, "result", "failed").increment();
            log.warn("STOMP /topic/avatar 발행 대기열에 넣지 못함");
        }
    }

    private ObjectNode coalescePending(ObjectNode older, ObjectNode newer) {
        if (!Objects.equals(older.get("input_provenance"), newer.get("input_provenance"))) {
            return newer;
        }
        Map<String, JsonNode> merged = new LinkedHashMap<>();
        for (JsonNode avatar : older.path("avatars")) {
            String worldId = avatar.path("world_id").asString(null);
            if (worldId != null && latestPoses.containsKey(worldId)) {
                merged.put(worldId, avatar);
            }
        }
        for (JsonNode avatar : newer.path("avatars")) {
            String worldId = avatar.path("world_id").asString(null);
            if (worldId != null) {
                merged.put(worldId, avatar);
            }
        }
        if (merged.isEmpty() || merged.size() > MAX_PEOPLE_PER_MESSAGE) {
            return newer;
        }
        ObjectNode out = mapper.createObjectNode();
        out.set("input_provenance", newer.get("input_provenance"));
        ArrayNode avatars = out.putArray("avatars");
        merged.values().forEach(avatars::add);
        return outboundBytes(out) <= MAX_MESSAGE_BYTES ? out : newer;
    }

    private int outboundBytes(ObjectNode payload) {
        ObjectNode envelope = I1Envelope.outbound(mapper, clock.millis(), AVATAR_FRAME, AVATAR_FRAME_VERSION, payload);
        return mapper.writeValueAsBytes(envelope).length;
    }

    /**
     * /topic/world(world_state)에서 사라진 world_id 의 최신 포즈를 지우고, 30초 넘게 포즈가 없는 사람도 함께 지운다.
     */
    @TransactionalEventListener(fallbackExecution = true)
    public void onWorldState(WorldStateStored stored) {
        JsonNode tracks = stored.envelope().payload().path("tracks");
        if (!tracks.isArray()) {
            return;
        }
        Set<String> activeWorldIds = new HashSet<>();
        for (JsonNode track : tracks) {
            String worldId = track.path("world_id").asString(null);
            if (worldId != null) {
                activeWorldIds.add(worldId);
            }
        }
        synchronized (poseLock) {
            latestPoses.keySet().retainAll(activeWorldIds);
            evictExpiredLocked(clock.instant());
        }
    }

    /** 30초 넘게 새 포즈가 오지 않은 사람을 메모리에서 지운다. */
    public void evictExpiredPoses(Instant now) {
        synchronized (poseLock) {
            evictExpiredLocked(now);
        }
    }

    private void evictExpiredLocked(Instant now) {
        long cutoffMs = now.minus(POSE_TTL).toEpochMilli();
        latestPoses.entrySet().removeIf(entry -> entry.getValue().receivedAtMs() < cutoffMs);
    }

    public Optional<PersonPoseState> latestPose(String worldId) {
        synchronized (poseLock) {
            evictExpiredLocked(clock.instant());
            return Optional.ofNullable(latestPoses.get(worldId));
        }
    }

    public void clearState() {
        synchronized (poseLock) {
            latestPoses.clear();
            seenMsgIds.clear();
            pendingFrames.clear();
        }
        assets.evictCache();
    }

    private static boolean isNewer(int newRev, long newCaptureTimeMs, int curRev, long curCaptureTimeMs) {
        if (newRev != curRev) {
            return newRev > curRev;
        }
        return newCaptureTimeMs > curCaptureTimeMs;
    }

    private void count(String msgType, String result) {
        String tag = (AVATAR_POSE_BATCH.equals(msgType) || AVATAR_ASSET.equals(msgType) || AVATAR_FRAME.equals(msgType))
                ? msgType
                : OTHER;
        meters.counter(METRIC, "msg_type", tag, "result", result).increment();
    }

    private static String reason(RuntimeException e) {
        return e instanceof InvalidI1Message ? e.getMessage() : e.getClass().getSimpleName();
    }

    @Override
    public void close() {
        avatarExecutor.shutdownNow();
    }

    public record PersonPoseState(
            String worldId,
            long streamEpoch,
            int identityRevision,
            long captureTimeMs,
            long receivedAtMs,
            Instant receivedAt,
            String calibrationVersion,
            int appearanceVersion,
            String jointSource,
            JsonNode pose) {
    }
}
