package com.ssafy.simai.global.properties;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * publicBaseUrl 은 Tier 0 가 구성 묶음을 내려받는 EC2 공개 주소(https://<도메인>)다. 묶음 안 구성 요소 주소가 이것으로 시작한다.
 * Tier 0 는 https 주소만 받는다(ai/services/tier0/config_fetch.py). 비어 있으면 묶음을 발행하지 않는다(S15P31A101-120).
 */
@ConfigurationProperties("simai.config-bundle")
public record ConfigBundleProperties(String publicBaseUrl) {

    public ConfigBundleProperties {
        publicBaseUrl = publicBaseUrl == null ? "" : publicBaseUrl.strip().replaceAll("/+$", "");
    }
}
