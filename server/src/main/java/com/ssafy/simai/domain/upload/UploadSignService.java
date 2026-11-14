package com.ssafy.simai.domain.upload;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.properties.StorageProperties;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * POST /api/uploads/sign(명세 12.6, 14, S15P31A101-69·-325). 목적별로 형식·크기·해시를 검사하고 객체 키를 서버가 정해
 * 서명 주소를 낸다. 발급은 upload_grant 에 남긴다(올라온 키 검증은 media_clip 등을 받을 때 이 기록으로 한다).
 * <ul>
 * <li>클라이언트는 키를 고르지 못한다. 남의 객체를 덮어쓰지 못하게 하려는 것이다.</li>
 * <li>클립 키는 clip_id 로 정해져 다시 받아도 같다. 업로더는 시도마다 새 주소를 받는다(ai/services/tier0/clips/upload.py).
 * 같은 주체·같은 내용이면 기록의 만료만 늘린다. 내용이 다르면 앞선 발급이 만료된 뒤에만 새 내용으로 바꾸고(다시 자른 클립),
 * 아직 살아 있거나 이미 기록된(used_at) 클립이면 409 로 거부한다. 발급 기록이 된 뒤에만 서명한다.</li>
 * <li>아바타 자산(AVATAR_ASSET)은 (world_id, stream_epoch, appearance_version) 을 함께 보내면 버전별 경로로 고정하고,
 * 같은 키에 다른 내용은 만료 뒤에도 덮어쓰지 않는다(AGENTS 규칙 6).</li>
 * <li>형식·크기·해시는 서명에 묶여 저장소가 다른 내용을 거부한다(ObjectStorage).</li>
 * </ul>
 */
@Service
public class UploadSignService {

    private static final Logger log = LoggerFactory.getLogger(UploadSignService.class);
    private static final Pattern SHA256 = Pattern.compile("^[0-9a-f]{64}$");
    private static final Pattern WORLD_ID = Pattern.compile("^[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,63}$");
    private static final Pattern AVATAR_FILE = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$");
    /** Tier 0 사람 지운 배경 묶음 폴더 이름(계약 static_reconstruct_submit bundle_id) */
    private static final Pattern BUNDLE_ID = Pattern.compile("^bundle-[0-9a-f]{32}$");
    /** 정적 3D 입력 이름(확장자 없음): 카메라 ID(계약 camera_id) 또는 calibration */
    private static final Pattern STATIC_INPUT_NAME = Pattern.compile("^[A-Za-z0-9_-]{1,64}$");

    private final ObjectStorage storage;
    private final NamedParameterJdbcTemplate jdbc;
    private final Clock clock;
    private final Duration uploadTtl;

    UploadSignService(ObjectStorage storage, NamedParameterJdbcTemplate jdbc, Clock clock, StorageProperties properties) {
        this.storage = storage;
        this.jdbc = jdbc;
        this.clock = clock;
        this.uploadTtl = properties.uploadTtl();
    }

    @Transactional
    SignedUpload sign(Requester who, SignRequest request) {
        UploadPurpose purpose = UploadPurpose.parse(request.purpose())
                .orElseThrow(() -> new BusinessException(ErrorCode.Storage.INVALID_UPLOAD));
        if (!purpose.allows(who.kind())) {
            throw new BusinessException(ErrorCode.Storage.UPLOAD_NOT_ALLOWED);
        }
        String contentType = request.contentType() == null ? "" : request.contentType().strip().toLowerCase(Locale.ROOT);
        String extension = purpose.extension(contentType)
                .orElseThrow(() -> new BusinessException(ErrorCode.Storage.INVALID_UPLOAD));
        long size = request.sizeBytes() == null ? 0 : request.sizeBytes();
        String sha256 = request.sha256() == null ? "" : request.sha256().strip().toLowerCase(Locale.ROOT);
        if (size < 1 || size > purpose.maxBytes() || !SHA256.matcher(sha256).matches()) {
            throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
        }
        String key = key(purpose, request, extension);
        if (!storage.configured()) {
            throw new BusinessException(ErrorCode.Storage.STORAGE_NOT_CONFIGURED);
        }
        Instant now = clock.instant().truncatedTo(ChronoUnit.MILLIS);

        UUID eventId = (purpose == UploadPurpose.TICKET_PHOTO || purpose == UploadPurpose.AVATAR_ASSET
                || purpose == UploadPurpose.VIDEO_SEGMENT || purpose == UploadPurpose.RECORDING_SEGMENT
                || purpose == UploadPurpose.SCENE_GLTF
                || purpose == UploadPurpose.STATIC_RECONSTRUCT_INPUT || purpose == UploadPurpose.REPORT_SIGNATURE)
                ? null
                : request.eventId();
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("key", key)
                .addValue("purpose", purpose.name())
                .addValue("requester", who.kind().stored())
                .addValue("device", who.deviceId())
                .addValue("user", who.userId())
                .addValue("worker", who.workerId())
                .addValue("contentType", contentType)
                .addValue("size", size)
                .addValue("sha256", sha256)
                .addValue("eventId", eventId)
                .addValue("issuedAt", Timestamp.from(now))
                .addValue("expiresAt", Timestamp.from(now.plus(uploadTtl)));
        // 같은 키(클립·아바타 자산 재시도)는 같은 목적·주체이고 아직 기록되지 않았을 때만 다시 발급한다.
        // 아바타 자산·정적 3D 입력(묶음별 키)은 만료 뒤에도 다른 내용으로 덮어쓰지 않는다(규칙 6).
        List<Long> granted = jdbc.queryForList("""
                INSERT INTO upload_grant (object_key, purpose, requester, requested_by_device, requested_by_user,
                    requested_by_worker, content_type, size_bytes, sha256, event_id, issued_at, expires_at)
                VALUES (:key, :purpose, :requester, :device, :user, :worker, :contentType, :size, :sha256, :eventId, :issuedAt, :expiresAt)
                ON CONFLICT (object_key) DO UPDATE SET issued_at = EXCLUDED.issued_at, expires_at = EXCLUDED.expires_at,
                    content_type = EXCLUDED.content_type, size_bytes = EXCLUDED.size_bytes, sha256 = EXCLUDED.sha256
                WHERE upload_grant.used_at IS NULL
                  AND upload_grant.purpose = EXCLUDED.purpose
                  AND upload_grant.requester = EXCLUDED.requester
                  AND upload_grant.requested_by_device IS NOT DISTINCT FROM EXCLUDED.requested_by_device
                  AND upload_grant.requested_by_user IS NOT DISTINCT FROM EXCLUDED.requested_by_user
                  AND upload_grant.requested_by_worker IS NOT DISTINCT FROM EXCLUDED.requested_by_worker
                  AND ((upload_grant.content_type = EXCLUDED.content_type
                        AND upload_grant.size_bytes = EXCLUDED.size_bytes
                        AND upload_grant.sha256 = EXCLUDED.sha256)
                       OR (upload_grant.purpose NOT IN ('AVATAR_ASSET', 'STATIC_RECONSTRUCT_INPUT')
                           AND upload_grant.expires_at <= EXCLUDED.issued_at))
                RETURNING grant_id
                """, params, Long.class);
        if (granted.isEmpty()) {
            log.warn("업로드 키 충돌 거부: purpose={} key={} requester={} sizeBytes={} sha256={}",
                    purpose.name(), key, who.kind().stored(), size, sha256);
            throw new BusinessException(ErrorCode.Storage.UPLOAD_KEY_CONFLICT);
        }
        // 서명이 실패하면(자격 증명 없음 등 503) 트랜잭션이 되돌아가 발급 기록도 남지 않는다
        SignedRequest signed = storage.signPut(key, contentType, size, sha256);
        return new SignedUpload(key, storage.uri(key), "PUT", signed.url(), signed.headers(),
                signed.expiresAt().toEpochMilli());
    }

    /** 버킷 안 경로. 계약 object_key 처럼 스킴·쿼리·앞 슬래시가 없다 */
    private static String key(UploadPurpose purpose, SignRequest request, String extension) {
        return switch (purpose) {
            case EVENT_CLIP -> {
                if (request.eventId() == null || request.clipId() == null) {
                    throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
                }
                yield purpose.prefix() + "/" + request.eventId() + "/" + request.clipId() + extension;
            }
            case EVENT_FRAME -> {
                if (request.eventId() == null) {
                    throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
                }
                yield purpose.prefix() + "/" + request.eventId() + "/" + UUID.randomUUID() + extension;
            }
            case VIDEO_SEGMENT, RECORDING_SEGMENT -> {
                // 키는 segment_id 로 정해져 다시 받아도 같다(클립처럼 재시도마다 새 주소를 받는다)
                // 기록(video_segment)은 uuid_v7 만 받는다. 다른 판이면 올려도 기록이 거부돼 고아 객체만 남는다
                if (request.segmentId() == null || request.segmentId().version() != 7) {
                    throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
                }
                yield purpose.prefix() + "/" + request.segmentId() + extension;
            }
            case TICKET_PHOTO, SCENE_GLTF, REPORT_SIGNATURE -> purpose.prefix() + "/" + UUID.randomUUID() + extension;
            case AVATAR_ASSET -> avatarKey(purpose, request, extension);
            case STATIC_RECONSTRUCT_INPUT -> staticInputKey(purpose, request, extension);
        };
    }

    /** bundle_id·file_name 은 함께 주거나 함께 뺀다. 주면 키가 정해져 같은 묶음을 다시 올려도 객체가 늘지 않는다 */
    private static String staticInputKey(UploadPurpose purpose, SignRequest request, String extension) {
        if (request.bundleId() == null && request.fileName() == null) {
            return purpose.prefix() + "/" + UUID.randomUUID() + "/" + UUID.randomUUID() + extension;
        }
        if (request.bundleId() == null || request.fileName() == null || !BUNDLE_ID.matcher(request.bundleId()).matches()
                || !STATIC_INPUT_NAME.matcher(request.fileName()).matches()) {
            throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
        }
        return purpose.prefix() + "/" + request.bundleId() + "/" + request.fileName() + extension;
    }

    private static String avatarKey(UploadPurpose purpose, SignRequest request, String extension) {
        boolean hasWorld = request.worldId() != null;
        boolean hasEpoch = request.streamEpoch() != null;
        boolean hasVersion = request.appearanceVersion() != null;
        if (!hasWorld && !hasEpoch && !hasVersion) {
            if (request.fileName() != null) {
                throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
            }
            return purpose.prefix() + "/" + UUID.randomUUID() + extension;
        }
        if (!hasWorld || !hasEpoch || !hasVersion) {
            throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
        }
        String worldId = request.worldId();
        long epoch = request.streamEpoch();
        int version = request.appearanceVersion();
        if (!WORLD_ID.matcher(worldId).matches() || worldId.contains("..") || epoch < 0 || version < 0) {
            throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
        }
        String leaf = avatarLeafName(request.fileName(), extension);
        return purpose.prefix() + "/" + worldId + "/e" + epoch + "/v" + version + "/" + leaf;
    }

    private static String avatarLeafName(String fileName, String extension) {
        if (fileName != null && !fileName.isBlank()) {
            if (!AVATAR_FILE.matcher(fileName).matches() || fileName.contains("..") || !fileName.endsWith(extension)) {
                throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
            }
            return fileName;
        }
        return switch (extension) {
            case ".glb" -> "mesh.glb";
            case ".gltf" -> "mesh.gltf";
            default -> "albedo" + extension;
        };
    }

    /**
     * sha256 은 올릴 내용의 SHA-256 16진수. clip_id 는 EVENT_CLIP, event_id 는 EVENT_*, segment_id 는 VIDEO_SEGMENT·RECORDING_SEGMENT 에 필요하고,
     * AVATAR_ASSET 은 선택으로 world_id·stream_epoch·appearance_version·file_name 을 함께 보낼 수 있다.
     * STATIC_RECONSTRUCT_INPUT 은 선택으로 bundle_id(bundle-<32자리 16진수>)와 file_name(확장자 없는 카메라 ID·calibration)을 함께 보낸다.
     */
    public record SignRequest(
            @JsonProperty("purpose") String purpose,
            @JsonProperty("content_type") String contentType,
            @JsonProperty("size_bytes") Long sizeBytes,
            @JsonProperty("sha256") String sha256,
            @JsonProperty("event_id") UUID eventId,
            @JsonProperty("clip_id") UUID clipId,
            @JsonProperty("world_id") String worldId,
            @JsonProperty("stream_epoch") Long streamEpoch,
            @JsonProperty("appearance_version") Integer appearanceVersion,
            @JsonProperty("file_name") String fileName,
            @JsonProperty("segment_id") UUID segmentId,
            @JsonProperty("bundle_id") String bundleId) {
    }

    /**
     * upload_url 로 method(PUT) 하며 headers 를 그대로 싣는다(서명에 묶여 있어 빼거나 바꾸면 저장소가 거부한다).
     * 메시지에는 object_key(media_clip) 또는 object_uri(event_frame_ready·avatar_asset) 를 싣고 서명 주소는 싣지 않는다.
     */
    public record SignedUpload(
            @JsonProperty("object_key") String objectKey,
            @JsonProperty("object_uri") String objectUri,
            @JsonProperty("method") String method,
            @JsonProperty("upload_url") String uploadUrl,
            @JsonProperty("headers") Map<String, String> headers,
            @JsonProperty("expires_at_ms") long expiresAtMs) {

        @Override
        public String toString() {
            return "SignedUpload[expiresAtMs=" + expiresAtMs + "]";
        }
    }
}
