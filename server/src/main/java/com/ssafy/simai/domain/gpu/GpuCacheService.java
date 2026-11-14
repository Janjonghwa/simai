package com.ssafy.simai.domain.gpu;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.ObjectHead;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

/**
 * GPU 캐시 서명(S15P31A101-700, docs/design/S15P31A101-700-gpu-auto-boot.md). GPU 인스턴스가 부팅 때 만들거나 받는 산출물
 * (TensorRT 엔진, vLLM 컴파일 캐시, Re-ID 가중치 묶음)을 객체 저장소 {@code gpu-cache/<kind>/<cache_key>.tar} 에 두고 다음 인스턴스가 받는다.
 * <ul>
 * <li>Pod 에 AWS 키를 두지 않는다. 그 GPU 세션의 워커 토큰(등록 응답에서 받고 세션이 끝나면 폐기)으로 한 번에 한 객체의 짧은 서명 주소만 받는다.</li>
 * <li>열린 GPU 세션에 묶인 워커만 받는다(다른 분석 워커 토큰은 403).</li>
 * <li>키는 인스턴스가 내용 조합(GPU 종류·TensorRT·모델 해시 등)으로 정하고 서버는 형식만 본다. 같은 키는 덮어쓰지 않는다: 이미 있으면 PUT 대신
 *     있는 객체 정보를 주고, 서명에도 If-None-Match: * 를 묶어 동시에 올려도 먼저 올린 것만 남는다(AGENTS 규칙 6).</li>
 * <li>본문은 계약 메시지가 아니라 이 API 전용 JSON 이다(워커 객체 서명 /api/worker/jobs/{id}/objects/sign 과 같은 성격).</li>
 * </ul>
 */
@Service
public class GpuCacheService {

    private static final Logger log = LoggerFactory.getLogger(GpuCacheService.class);
    static final String PREFIX = "gpu-cache";
    static final Set<String> KINDS = Set.of("trt-engine", "vllm-compile", "reid");
    /** 소문자·숫자·._- 조각 1~6개를 / 로 잇는다. 조각은 .·- 로 시작하지 않는다(.. 경로 금지) */
    private static final Pattern CACHE_KEY = Pattern.compile("^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63}){0,5}$");
    private static final Pattern SHA256 = Pattern.compile("^[0-9a-f]{64}$");
    static final String CONTENT_TYPE = "application/x-tar";
    /** S3 한 번 PUT 상한(5 GiB) 안. Re-ID 묶음 약 1 GB, vLLM 컴파일 캐시·엔진은 그보다 작다 */
    static final long MAX_BYTES = 4L << 30;

    private final ObjectStorage storage;
    private final GpuSessionService sessions;

    GpuCacheService(ObjectStorage storage, GpuSessionService sessions) {
        this.storage = storage;
        this.sessions = sessions;
    }

    CacheSign sign(String workerId, SignRequest request) {
        if (workerId == null || !sessions.openSessionForWorker(workerId)) {
            throw new BusinessException(ErrorCode.Storage.UPLOAD_NOT_ALLOWED);
        }
        String kind = request.kind() == null ? "" : request.kind();
        String cacheKey = request.cacheKey() == null ? "" : request.cacheKey();
        String method = request.method() == null ? "" : request.method().toUpperCase(Locale.ROOT);
        if (!KINDS.contains(kind) || !CACHE_KEY.matcher(cacheKey).matches() || cacheKey.contains("..")
                || !(method.equals("GET") || method.equals("PUT"))) {
            throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
        }
        String key = PREFIX + "/" + kind + "/" + cacheKey + ".tar";
        if (!storage.configured()) {
            throw new BusinessException(ErrorCode.Storage.STORAGE_NOT_CONFIGURED);
        }
        Optional<ObjectHead> head = storage.head(key);
        if (method.equals("GET")) {
            if (head.isEmpty()) {
                return CacheSign.miss(key);
            }
            SignedRequest get = storage.signGet(key);
            return new CacheSign(key, true, head.get().sizeBytes(), head.get().sha256Hex(), "GET", get.url(), get.headers(),
                    get.expiresAt().toEpochMilli());
        }
        long size = request.sizeBytes() == null ? 0 : request.sizeBytes();
        String sha256 = request.sha256() == null ? "" : request.sha256().strip().toLowerCase(Locale.ROOT);
        if (size < 1 || size > MAX_BYTES || !SHA256.matcher(sha256).matches()) {
            throw new BusinessException(ErrorCode.Storage.INVALID_UPLOAD);
        }
        if (head.isPresent()) {
            // 먼저 올린 것이 이긴다. 인스턴스는 올리지 않고 넘어간다
            return new CacheSign(key, true, head.get().sizeBytes(), head.get().sha256Hex(), null, null, null, null);
        }
        SignedRequest put = storage.signPut(key, CONTENT_TYPE, size, sha256, true);
        log.info("GPU 캐시 올리기 서명: kind={} worker={} sizeBytes={}", kind, workerId, size);
        return new CacheSign(key, false, null, null, "PUT", put.url(), put.headers(), put.expiresAt().toEpochMilli());
    }

    /** method GET: 있으면 받을 주소. PUT: 없을 때만 올릴 주소(size_bytes·sha256 필수, 서명에 묶임) */
    public record SignRequest(
            @JsonProperty("kind") String kind,
            @JsonProperty("cache_key") String cacheKey,
            @JsonProperty("method") String method,
            @JsonProperty("size_bytes") Long sizeBytes,
            @JsonProperty("sha256") String sha256) {
    }

    /**
     * hit: 객체가 이미 있음. GET 이면 url 로 받고 sha256(저장소가 기록한 내용 해시, 모르면 null)으로 확인한다. PUT 이 hit 면 올리지 않는다.
     * url·headers 는 비밀 값처럼 다룬다(로그 금지). headers 는 그대로 실어야 한다(서명에 묶임).
     */
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record CacheSign(
            @JsonProperty("object_key") String objectKey,
            @JsonProperty("hit") boolean hit,
            @JsonProperty("size_bytes") Long sizeBytes,
            @JsonProperty("sha256") String sha256,
            @JsonProperty("method") String method,
            @JsonProperty("url") String url,
            @JsonProperty("headers") Map<String, String> headers,
            @JsonProperty("expires_at_ms") Long expiresAtMs) {

        static CacheSign miss(String key) {
            return new CacheSign(key, false, null, null, null, null, null, null);
        }

        @Override
        public String toString() {
            return "CacheSign[objectKey=" + objectKey + ", hit=" + hit + ", method=" + method + "]";
        }
    }
}
