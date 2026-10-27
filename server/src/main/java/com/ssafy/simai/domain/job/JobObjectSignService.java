package com.ssafy.simai.domain.job;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.UUID;
import java.util.regex.Pattern;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 분석 워커 전용 객체 서명(POST /api/worker/jobs/{job_id}/objects/sign, 명세 12.5·14, S15P31A101-248).
 * 작업 input 에는 서명 주소를 넣지 않으므로(계약) 워커는 대여 중에 이 API 로 주소를 받는다.
 * <ul>
 * <li>그 워커의 그 작업 대여가 지금 유효할 때만 서명한다(ACTIVE, 만료 전, 작업이 LEASED). 아니면 409 LEASE_NOT_ACTIVE.</li>
 * <li>GET 은 그 작업 input 안에 그대로 적힌 s3:// 주소(프레임·calibration_ref 등)만. 다른 객체는 403.</li>
 * <li>PUT 키는 EC2 가 정한다: {작업별 앞부분}/{job_id}/{lease_id}/{이름}. 워커는 이름만 고른다. 형식·크기·해시는 서명에 묶는다.
 * 대여마다 키가 달라, 대여를 잃은 워커가 남은 서명 주소로 늦게 올려도 재할당된 시도의 결과를 덮어쓰지 못한다.</li>
 * </ul>
 * 대여 상태는 바꾸지 않는다(만료 처리·재할당은 JobLeaseService). 서명만 하므로 발급 기록은 남기지 않는다.
 */
@Service
public class JobObjectSignService {

    /** 결과 객체 이름. 경로 구분자·상위 경로가 들어가지 않게 한 단계 이름만 받는다 */
    private static final Pattern NAME = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$");
    private static final Pattern SHA256 = Pattern.compile("^[0-9a-f]{64}$");
    private static final Pattern CONTENT_TYPE = Pattern.compile("^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$");
    /** 정적 3D glTF·점군까지 들어가는 상한 */
    private static final long MAX_PUT_BYTES = 512L << 20;
    /** 작업 유형별 결과 키 앞부분. 없는 유형은 job-objects/{job_type} */
    private static final Map<String, String> PUT_PREFIX = Map.of(
            "bev_floorplan", "floor-plans",
            "static_reconstruct", "static-3d");

    private final ObjectStorage storage;
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    JobObjectSignService(ObjectStorage storage, NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.storage = storage;
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    @Transactional(readOnly = true)
    SignedObject sign(String workerId, String jobId, SignRequest request, Instant now) {
        UUID leaseId = request.leaseId();
        String op = request.op() == null ? "" : request.op().strip().toUpperCase(Locale.ROOT);
        if (leaseId == null || !(op.equals("GET") || op.equals("PUT"))) {
            throw new BusinessException(ErrorCode.Job.INVALID_OBJECT_REQUEST);
        }
        LeasedJob job = activeLease(workerId, jobId, leaseId, now)
                .orElseThrow(() -> new BusinessException(ErrorCode.Job.LEASE_NOT_ACTIVE));
        if (!storage.configured()) {
            throw new BusinessException(ErrorCode.Storage.STORAGE_NOT_CONFIGURED);
        }
        return op.equals("GET") ? signGet(job, request) : signPut(job, leaseId, request);
    }

    private SignedObject signGet(LeasedJob job, SignRequest request) {
        String uri = request.uri() == null ? "" : request.uri();
        String prefix = storage.uri("");
        // input 에 같은 문자열로 적힌 우리 버킷 주소만. 다른 버킷·주소를 고쳐 쓴 요청은 받지 않는다
        if (!uri.startsWith(prefix) || uri.length() == prefix.length() || !containsUri(job.input(), uri)) {
            throw new BusinessException(ErrorCode.Job.OBJECT_NOT_IN_INPUT);
        }
        SignedRequest signed = storage.signGet(uri.substring(prefix.length()));
        return new SignedObject("GET", uri, "GET", signed.url(), signed.headers(), signed.expiresAt().toEpochMilli());
    }

    private SignedObject signPut(LeasedJob job, UUID leaseId, SignRequest request) {
        String name = request.name() == null ? "" : request.name();
        String contentType = request.contentType() == null ? "" : request.contentType().strip().toLowerCase(Locale.ROOT);
        long size = request.sizeBytes() == null ? 0 : request.sizeBytes();
        String sha256 = request.sha256() == null ? "" : request.sha256().strip().toLowerCase(Locale.ROOT);
        if (!NAME.matcher(name).matches() || name.contains("..") || !CONTENT_TYPE.matcher(contentType).matches()
                || size < 1 || size > MAX_PUT_BYTES || !SHA256.matcher(sha256).matches()) {
            throw new BusinessException(ErrorCode.Job.INVALID_OBJECT_REQUEST);
        }
        String key = PUT_PREFIX.getOrDefault(job.jobType(), "job-objects/" + job.jobType()) + "/" + job.jobId() + "/" + leaseId
                + "/" + name;
        SignedRequest signed = storage.signPut(key, contentType, size, sha256);
        return new SignedObject("PUT", storage.uri(key), "PUT", signed.url(), signed.headers(),
                signed.expiresAt().toEpochMilli());
    }

    private Optional<LeasedJob> activeLease(String workerId, String jobId, UUID leaseId, Instant now) {
        return jdbc.query("""
                SELECT j.job_id, j.job_type, j.input::text AS input, l.lease_expires_at
                FROM job_lease l JOIN analysis_job j ON j.job_id = l.job_id
                WHERE l.lease_id = :leaseId AND l.job_id = :jobId AND l.worker_id = :workerId
                  AND l.status = 'ACTIVE' AND j.status = 'LEASED'
                """, new MapSqlParameterSource().addValue("leaseId", leaseId).addValue("jobId", jobId)
                .addValue("workerId", workerId), (rs, i) -> {
                    // 만료 처리 전이라도 만료 시각이 지난 대여로는 서명하지 않는다
                    if (!now.isBefore(rs.getObject("lease_expires_at", OffsetDateTime.class).toInstant())) {
                        return null;
                    }
                    return new LeasedJob(rs.getString("job_id"), rs.getString("job_type"), mapper.readTree(rs.getString("input")));
                }).stream().filter(Objects::nonNull).findFirst();
    }

    /** input 의 어느 깊이든 값이 uri 와 똑같은 문자열이 있는지 */
    private static boolean containsUri(JsonNode node, String uri) {
        if (node.isString()) {
            return uri.equals(node.stringValue());
        }
        for (JsonNode child : node) {
            if (containsUri(child, uri)) {
                return true;
            }
        }
        return false;
    }

    private record LeasedJob(String jobId, String jobType, JsonNode input) {
    }

    /** GET 은 uri, PUT 은 name·content_type·size_bytes·sha256(올릴 내용의 SHA-256 16진수) */
    public record SignRequest(
            @JsonProperty("lease_id") UUID leaseId,
            @JsonProperty("op") String op,
            @JsonProperty("uri") String uri,
            @JsonProperty("name") String name,
            @JsonProperty("content_type") String contentType,
            @JsonProperty("size_bytes") Long sizeBytes,
            @JsonProperty("sha256") String sha256) {
    }

    /**
     * url 로 method 하며 headers 를 그대로 싣는다(PUT 은 서명에 묶여 빼거나 바꾸면 저장소가 거부한다).
     * uri 는 그 객체의 s3:// 주소다. PUT 이면 결과(job_callback)에 이 주소를 싣는다.
     */
    public record SignedObject(
            @JsonProperty("op") String op,
            @JsonProperty("uri") String uri,
            @JsonProperty("method") String method,
            @JsonProperty("url") String url,
            @JsonProperty("headers") Map<String, String> headers,
            @JsonProperty("expires_at_ms") long expiresAtMs) {

        @Override
        public String toString() {
            return "SignedObject[op=" + op + ", uri=" + uri + ", expiresAtMs=" + expiresAtMs + "]";
        }
    }
}
