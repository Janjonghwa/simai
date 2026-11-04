package com.ssafy.simai.global.storage;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.properties.StorageProperties;
import java.net.URI;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.Collections;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.OptionalLong;
import java.util.function.Supplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.awscore.exception.AwsServiceException;
import software.amazon.awssdk.awscore.presigner.PresignedRequest;
import software.amazon.awssdk.core.ResponseBytes;
import software.amazon.awssdk.core.exception.SdkException;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3ClientBuilder;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.model.ChecksumMode;
import software.amazon.awssdk.services.s3.model.DeleteObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;
import software.amazon.awssdk.services.s3.model.HeadObjectRequest;
import software.amazon.awssdk.services.s3.model.HeadObjectResponse;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;
import software.amazon.awssdk.services.s3.presigner.model.GetObjectPresignRequest;
import software.amazon.awssdk.services.s3.presigner.model.PutObjectPresignRequest;

/**
 * 비공개 객체 저장소의 임시 서명 주소(명세 14, S15P31A101-69). 서명은 EC2 안에서 계산만 하고 저장소에 요청하지 않는다.
 * 올리기(PUT)는 형식·크기·내용 해시(x-amz-checksum-sha256)를 서명에 묶어, 다른 내용이나 크기로는 저장소가 거부한다.
 * 만료는 저장소가 판단한다(만료 뒤 요청은 403). 서명 주소는 로그에 남기지 않는다.
 * 지우기(delete)만 EC2 가 저장소를 직접 부른다(보관 기간 자동 삭제, S15P31A101-72).
 */
@Component
public class ObjectStorage implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(ObjectStorage.class);

    private final StorageProperties properties;
    private final S3Presigner presigner;
    private final S3Client client;

    public ObjectStorage(StorageProperties properties) {
        this.properties = properties;
        if (properties.bucket().isEmpty()) {
            log.warn("simai.storage.bucket(SIMAI_STORAGE_BUCKET) 이 비어 업로드·조회 서명 주소를 내지 않는다");
            this.presigner = null;
            this.client = null;
            return;
        }
        Region region = Region.of(properties.region());
        S3Configuration s3 = S3Configuration.builder().pathStyleAccessEnabled(properties.pathStyle()).build();
        // EC2 인스턴스 역할 등. 서명·호출할 때 처음 찾는다(관리 명령처럼 쓰지 않는 프로세스는 찾지 않는다)
        AwsCredentialsProvider credentials = properties.accessKey().isEmpty()
                ? DefaultCredentialsProvider.builder().build()
                : StaticCredentialsProvider.create(AwsBasicCredentials.create(properties.accessKey(), properties.secretKey()));
        S3Presigner.Builder presignerBuilder = S3Presigner.builder().region(region).serviceConfiguration(s3)
                .credentialsProvider(credentials);
        // 저장소가 멈춰도 삭제 작업이 오래 붙잡히지 않게 호출 시간을 묶는다(재시도 포함 30초, 한 번 10초)
        S3ClientBuilder clientBuilder = S3Client.builder().region(region).serviceConfiguration(s3).credentialsProvider(credentials)
                .overrideConfiguration(o -> o.apiCallTimeout(Duration.ofSeconds(30)).apiCallAttemptTimeout(Duration.ofSeconds(10)));
        if (!properties.endpoint().isEmpty()) {
            presignerBuilder.endpointOverride(URI.create(properties.endpoint()));
            clientBuilder.endpointOverride(URI.create(properties.endpoint()));
        }
        this.presigner = presignerBuilder.build();
        this.client = clientBuilder.build();
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

    /**
     * 객체를 지운다. 없는 키도 성공이다(S3 DeleteObject 는 멱등). 실패하면 STORAGE_UNAVAILABLE.
     * 경고 로그에는 키 대신 저장소의 상태·오류 코드만 남긴다(키에는 이벤트·클립 식별자가 있다).
     * 버킷 버전 관리를 켜 두면 지운 표시만 남고 이전 판이 남으므로, 운영 버킷은 버전 관리를 끈다(server-data-retention.md).
     */
    public void delete(String key) {
        requireConfigured();
        try {
            client.deleteObject(DeleteObjectRequest.builder().bucket(properties.bucket()).key(key).build());
        } catch (AwsServiceException e) {
            log.warn("객체 저장소에서 객체를 지우지 못했다: status={} code={}", e.statusCode(),
                    e.awsErrorDetails() == null ? null : e.awsErrorDetails().errorCode());
            throw new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE);
        } catch (SdkException e) {
            log.warn("객체 저장소에 닿지 못해 객체를 지우지 못했다: {}", e.getClass().getSimpleName());
            throw new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE);
        }
    }

    /**
     * 객체 크기(HEAD). 없으면 빈 값. 서명 주소만 내고 올렸는지 모르는 객체를 화면 전체가 쓰는 곳(현재 Scene)에 걸기 전에 본다(S15P31A101-322).
     * 저장소에 닿지 못하거나 권한이 없으면 STORAGE_UNAVAILABLE. 로그에는 키를 남기지 않는다.
     */
    public OptionalLong size(String key) {
        requireConfigured();
        try {
            return OptionalLong.of(client.headObject(HeadObjectRequest.builder().bucket(properties.bucket()).key(key).build())
                    .contentLength());
        } catch (AwsServiceException e) {
            if (e.statusCode() == 404) {
                return OptionalLong.empty();
            }
            log.warn("객체 저장소에서 객체 크기를 읽지 못했다: status={} code={}", e.statusCode(),
                    e.awsErrorDetails() == null ? null : e.awsErrorDetails().errorCode());
            throw new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE);
        } catch (SdkException e) {
            log.warn("객체 저장소에 닿지 못해 객체 크기를 읽지 못했다: {}", e.getClass().getSimpleName());
            throw new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE);
        }
    }

    /**
     * 객체 크기와 저장소가 기록한 SHA-256(올릴 때 x-amz-checksum-sha256 을 서명에 묶은 객체만, 16진수)(HEAD). 없으면 빈 값.
     * 체크섬을 기록하지 않은 저장소·객체는 sha256Hex 가 null 이다(모름). 세팅 결과 GLB 를 Scene 후보로 등록하기 전에 본다(S15P31A101-467).
     */
    public Optional<ObjectHead> head(String key) {
        requireConfigured();
        try {
            HeadObjectResponse head = client.headObject(HeadObjectRequest.builder().bucket(properties.bucket()).key(key)
                    .checksumMode(ChecksumMode.ENABLED).build());
            String checksum = head.checksumSHA256();
            String hex = null;
            // 여러 조각으로 올린 객체의 체크섬은 "값-조각수" 꼴(조각 체크섬의 체크섬)이라 내용 해시와 비교할 수 없다
            if (checksum != null && !checksum.contains("-")) {
                try {
                    hex = HexFormat.of().formatHex(Base64.getDecoder().decode(checksum));
                } catch (IllegalArgumentException e) {
                    hex = null;
                }
            }
            return Optional.of(new ObjectHead(head.contentLength(), hex));
        } catch (AwsServiceException e) {
            if (e.statusCode() == 404) {
                return Optional.empty();
            }
            log.warn("객체 저장소에서 객체 정보를 읽지 못했다: status={} code={}", e.statusCode(),
                    e.awsErrorDetails() == null ? null : e.awsErrorDetails().errorCode());
            throw new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE);
        } catch (SdkException e) {
            log.warn("객체 저장소에 닿지 못해 객체 정보를 읽지 못했다: {}", e.getClass().getSimpleName());
            throw new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE);
        }
    }

    /**
     * 작은 객체(보정 문서 등)의 바이트를 EC2 가 직접 읽는다(S15P31A101-467). 없거나 maxBytes 를 넘으면 빈 값.
     * 앞 maxBytes+1 바이트만 받아(Range) 큰 객체를 통째로 받지 않는다. 로그에는 키를 남기지 않는다.
     */
    public Optional<byte[]> read(String key, int maxBytes) {
        requireConfigured();
        try {
            ResponseBytes<GetObjectResponse> bytes = client.getObjectAsBytes(GetObjectRequest.builder()
                    .bucket(properties.bucket()).key(key).range("bytes=0-" + maxBytes).build());
            byte[] content = bytes.asByteArray();
            return content.length > maxBytes ? Optional.empty() : Optional.of(content);
        } catch (AwsServiceException e) {
            // 404 없음, 416 빈 객체(범위 밖)
            if (e.statusCode() == 404 || e.statusCode() == 416) {
                return Optional.empty();
            }
            log.warn("객체 저장소에서 객체를 읽지 못했다: status={} code={}", e.statusCode(),
                    e.awsErrorDetails() == null ? null : e.awsErrorDetails().errorCode());
            throw new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE);
        } catch (SdkException e) {
            log.warn("객체 저장소에 닿지 못해 객체를 읽지 못했다: {}", e.getClass().getSimpleName());
            throw new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE);
        }
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
            log.warn("객체 저장소 서명 주소를 만들지 못했다: {}", e.getClass().getSimpleName());
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
            try {
                presigner.close();
            } finally {
                client.close();
            }
        }
    }

    /** HEAD 결과. sha256Hex 는 저장소가 기록한 내용 SHA-256(16진수), 모르면 null */
    public record ObjectHead(long sizeBytes, String sha256Hex) {
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
