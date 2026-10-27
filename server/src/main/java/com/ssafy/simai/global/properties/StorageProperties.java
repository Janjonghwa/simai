package com.ssafy.simai.global.properties;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 객체 저장소(S3 또는 S3 호환 Lightsail 버킷, 명세 3.1·14). 비공개 버킷이고 임시 서명 주소로만 올리고 읽는다(S15P31A101-69).
 * bucket 이 비어 있으면 서명 주소를 내지 않는다(503). 자격 증명은 기본으로 EC2 인스턴스 역할 등 AWS 기본 공급자에서 찾고,
 * accessKey·secretKey 는 로컬 S3 호환 저장소(MinIO 등)를 쓸 때만 넣는다. endpoint 도 그때만 넣는다(비우면 AWS 지역 주소).
 */
@ConfigurationProperties("simai.storage")
public record StorageProperties(String bucket, String region, String endpoint, boolean pathStyle,
        String accessKey, String secretKey, Duration uploadTtl, Duration downloadTtl) {

    public StorageProperties {
        bucket = bucket == null ? "" : bucket.strip();
        region = region == null || region.isBlank() ? "ap-northeast-2" : region.strip();
        endpoint = endpoint == null ? "" : endpoint.strip();
        accessKey = accessKey == null ? "" : accessKey.strip();
        secretKey = secretKey == null ? "" : secretKey.strip();
        uploadTtl = uploadTtl == null ? Duration.ofMinutes(5) : uploadTtl;
        downloadTtl = downloadTtl == null ? Duration.ofMinutes(5) : downloadTtl;
    }

    @Override
    public String toString() {
        // 비밀 값이 로그·오류 메시지에 남지 않게 뺀다
        return "StorageProperties[bucket=" + bucket + ", region=" + region + ", endpoint=" + endpoint + "]";
    }
}
