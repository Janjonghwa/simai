package com.ssafy.simai.global.storage;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.properties.StorageProperties;
import java.net.URI;
import java.time.Instant;
import java.util.Base64;
import java.util.Collections;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.awscore.presigner.PresignedRequest;
import software.amazon.awssdk.core.exception.SdkException;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;
import software.amazon.awssdk.services.s3.presigner.model.GetObjectPresignRequest;
import software.amazon.awssdk.services.s3.presigner.model.PutObjectPresignRequest;

/**
 * 비공개 객체 저장소의 임시 서명 주소(명세 14, S15P31A101-69). 서명은 EC2 안에서 계산만 하고 저장소에 요청하지 않는다.
 * 올리기(PUT)는 형식·크기·내용 해시(x-amz-checksum-sha256)를 서명에 묶어, 다른 내용이나 크기로는 저장소가 거부한다.
 * 만료는 저장소가 판단한다(만료 뒤 요청은 403). 서명 주소는 로그에 남기지 않는다.
 */
@Component
public class ObjectStorage implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(ObjectStorage.class);

    private final StorageProperties properties;
    private final S3Presigner presigner;

    public ObjectStorage(StorageProperties properties) {
        this.properties = properties;
        if (properties.bucket().isEmpty()) {
            log.warn("simai.storage.bucket(SIMAI_STORAGE_BUCKET) 이 비어 업로드·조회 서명 주소를 내지 않는다");
            this.presigner = null;
            return;
        }
        S3Presigner.Builder builder = S3Presigner.builder()
                .region(Region.of(properties.region()))
                .serviceConfiguration(S3Configuration.builder().pathStyleAccessEnabled(properties.pathStyle()).build());
        if (!properties.endpoint().isEmpty()) {
            builder.endpointOverride(URI.create(properties.endpoint()));
        }
        if (!properties.accessKey().isEmpty()) {
            builder.credentialsProvider(StaticCredentialsProvider.create(
                    AwsBasicCredentials.create(properties.accessKey(), properties.secretKey())));
        } else {
            // EC2 인스턴스 역할 등. 서명할 때 처음 찾는다(관리 명령처럼 서명하지 않는 프로세스는 찾지 않는다)
            builder.credentialsProvider(DefaultCredentialsProvider.builder().build());
        }
        this.presigner = builder.build();
    }

    public boolean configured() {
        return presigner != null;
    }

    /** 메시지·DB 에 남기는 저장소 주소 s3://버킷/키(계약 object_uri). 만료되는 서명 주소가 아니다 */
    public String uri(String key) {
        return "s3://" + properties.bucket() + "/" + key;
    }

    /** sha256 은 16진수 64자. 저장소는 Base64 로 받는다 */
    public SignedRequest signPut(String key, String contentType, long sizeBytes, String sha256Hex) {
        requireConfigured();
        String checksum = Base64.getEncoder().encodeToString(HexFormat.of().parseHex(sha256Hex));
        PutObjectRequest put = PutObjectRequest.builder()
                .bucket(properties.bucket())
                .key(key)
                .contentType(contentType)
                .contentLength(sizeBytes)
                .checksumSHA256(checksum)
                .build();
        return sign(() -> presigner.presignPutObject(PutObjectPresignRequest.builder()
                .signatureDuration(properties.uploadTtl())
                .putObjectRequest(put)
                .build()));
    }

    public SignedRequest signGet(String key) {
        requireConfigured();
        GetObjectRequest get = GetObjectRequest.builder().bucket(properties.bucket()).key(key).build();
        return sign(() -> presigner.presignGetObject(GetObjectPresignRequest.builder()
                .signatureDuration(properties.downloadTtl())
                .getObjectRequest(get)
                .build()));
    }

    private void requireConfigured() {
        if (presigner == null) {
            throw new BusinessException(ErrorCode.Storage.STORAGE_NOT_CONFIGURED);
        }
    }

    private static SignedRequest sign(Supplier<? extends PresignedRequest> presign) {
        PresignedRequest signed;
        try {
            signed = presign.get();
        } catch (SdkException e) {
            // 자격 증명을 찾지 못함 등. 메시지에 서명 값은 없다
            log.warn("객체 저장소 서명 주소를 만들지 못했다: {}", e.getMessage());
            throw new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE);
        }
        // 클라이언트가 그대로 실어야 하는 서명된 헤더. host 는 주소에 들어 있다
        Map<String, String> headers = new LinkedHashMap<>();
        signed.signedHeaders().forEach((name, values) -> {
            if (!name.equalsIgnoreCase("host")) {
                headers.put(name, String.join(",", values));
            }
        });
        return new SignedRequest(signed.url().toString(), headers, signed.expiration());
    }

    @Override
    public void close() {
        if (presigner != null) {
            presigner.close();
        }
    }

    /** url·headers 는 비밀 값처럼 다룬다(로그 금지) */
    public record SignedRequest(String url, Map<String, String> headers, Instant expiresAt) {

        public SignedRequest {
            headers = Collections.unmodifiableMap(new LinkedHashMap<>(headers));
        }

        @Override
        public String toString() {
            return "SignedRequest[expiresAt=" + expiresAt + ", headers=" + List.copyOf(headers.keySet()) + "]";
        }
    }
}
