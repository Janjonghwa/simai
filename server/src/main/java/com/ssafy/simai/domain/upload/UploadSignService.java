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
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * POST /api/uploads/sign(명세 12.6, 14, S15P31A101-69). 목적별로 형식·크기·해시를 검사하고 객체 키를 서버가 정해
 * 서명 주소를 낸다. 발급은 upload_grant 에 남긴다(올라온 키 검증은 media_clip 등을 받을 때 이 기록으로 한다).
 * <ul>
 * <li>클라이언트는 키를 고르지 못한다. 남의 객체를 덮어쓰지 못하게 하려는 것이다.</li>
 * <li>클립 키는 clip_id 로 정해져 다시 받아도 같다. 업로더는 시도마다 새 주소를 받는다(ai/services/tier0/clips/upload.py).
 * 같은 주체·같은 내용이면 기록의 만료만 늘린다. 내용이 다르면 앞선 발급이 만료된 뒤에만 새 내용으로 바꾸고(다시 자른 클립),
 * 아직 살아 있거나 이미 기록된(used_at) 클립이면 409 로 거부한다. 발급 기록이 된 뒤에만 서명한다.</li>
 * <li>형식·크기·해시는 서명에 묶여 저장소가 다른 내용을 거부한다(ObjectStorage).</li>
 * </ul>
 */
@Service
public class UploadSignService {

    private static final Pattern SHA256 = Pattern.compile("^[0-9a-f]{64}$");

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

        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("key", key)
                .addValue("purpose", purpose.name())
                .addValue("requester", who.kind().name())
                .addValue("device", who.deviceId())
                .addValue("user", who.userId())
                .addValue("contentType", contentType)
                .addValue("size", size)
                .addValue("sha256", sha256)
                .addValue("eventId", purpose == UploadPurpose.TICKET_PHOTO ? null : request.eventId())
                .addValue("issuedAt", Timestamp.from(now))
                .addValue("expiresAt", Timestamp.from(now.plus(uploadTtl)));
        // 같은 키(클립 재시도)는 같은 목적·주체이고 아직 기록되지 않았을 때만 다시 발급한다. 내용은 같거나, 앞선 발급이
        // 만료됐을 때만 바꾼다. 아니면 행이 돌아오지 않는다
        List<Long> granted = jdbc.queryForList("""
                INSERT INTO upload_grant (object_key, purpose, requester, requested_by_device, requested_by_user,
                    content_type, size_bytes, sha256, event_id, issued_at, expires_at)
                VALUES (:key, :purpose, :requester, :device, :user, :contentType, :size, :sha256, :eventId, :issuedAt, :expiresAt)
                ON CONFLICT (object_key) DO UPDATE SET issued_at = EXCLUDED.issued_at, expires_at = EXCLUDED.expires_at,
                    content_type = EXCLUDED.content_type, size_bytes = EXCLUDED.size_bytes, sha256 = EXCLUDED.sha256
                WHERE upload_grant.used_at IS NULL
                  AND upload_grant.purpose = EXCLUDED.purpose
                  AND upload_grant.requester = EXCLUDED.requester
                  AND upload_grant.requested_by_device IS NOT DISTINCT FROM EXCLUDED.requested_by_device
                  AND upload_grant.requested_by_user IS NOT DISTINCT FROM EXCLUDED.requested_by_user
                  AND ((upload_grant.content_type = EXCLUDED.content_type
                        AND upload_grant.size_bytes = EXCLUDED.size_bytes
                        AND upload_grant.sha256 = EXCLUDED.sha256)
                       OR upload_grant.expires_at <= EXCLUDED.issued_at)
                RETURNING grant_id
                """, params, Long.class);
        if (granted.isEmpty()) {
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
            case TICKET_PHOTO -> purpose.prefix() + "/" + UUID.randomUUID() + extension;
        };
    }

    /** sha256 은 올릴 내용의 SHA-256 16진수. clip_id 는 EVENT_CLIP, event_id 는 EVENT_* 에 필요하다 */
    public record SignRequest(
            @JsonProperty("purpose") String purpose,
            @JsonProperty("content_type") String contentType,
            @JsonProperty("size_bytes") Long sizeBytes,
            @JsonProperty("sha256") String sha256,
            @JsonProperty("event_id") UUID eventId,
            @JsonProperty("clip_id") UUID clipId) {
    }

    /**
     * upload_url 로 method(PUT) 하며 headers 를 그대로 싣는다(서명에 묶여 있어 빼거나 바꾸면 저장소가 거부한다).
     * 메시지에는 object_key(media_clip) 또는 object_uri(event_frame_ready) 를 싣고 서명 주소는 싣지 않는다.
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
            return "SignedUpload[objectKey=" + objectKey + ", expiresAtMs=" + expiresAtMs + "]";
        }
    }
}
