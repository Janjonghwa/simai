package com.ssafy.simai.global.properties;

import java.util.Locale;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * tokenSha256 은 Tier 0 토큰(SIMAI_TIER0_TOKEN) 원문의 SHA-256 16진수다. 원문은 EC2 에 두지 않는다(명세 14).
 * 비어 있으면 I1 연결을 모두 거부한다. Tier 0 는 사이트에 하나라 테이블 대신 환경 변수로 둔다(S15P31A101-103).
 */
@ConfigurationProperties("simai.tier0")
public record Tier0Properties(String tokenSha256) {

    public Tier0Properties {
        tokenSha256 = tokenSha256 == null ? "" : tokenSha256.strip().toLowerCase(Locale.ROOT);
    }
}
