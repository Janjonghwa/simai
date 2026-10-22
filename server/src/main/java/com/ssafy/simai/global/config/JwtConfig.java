package com.ssafy.simai.global.config;

import com.nimbusds.jose.jwk.source.ImmutableSecret;
import com.ssafy.simai.global.properties.AuthProperties;
import java.util.Base64;
import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtValidators;
import org.springframework.security.oauth2.jwt.NimbusJwtDecoder;
import org.springframework.security.oauth2.jwt.NimbusJwtEncoder;

@Configuration
public class JwtConfig {

    /** 토큰 iss. 다른 서비스가 같은 키로 만든 토큰을 받지 않게 검사한다. */
    public static final String ISSUER = "simai";

    // HS256 키는 해시 출력 길이(32바이트) 이상이어야 한다(RFC 7518 3.2)
    private static final int MIN_SECRET_BYTES = 32;

    private final SecretKey key;

    JwtConfig(AuthProperties properties) {
        this.key = decodeKey(properties.jwt().secret());
    }

    @Bean
    JwtEncoder jwtEncoder() {
        return new NimbusJwtEncoder(new ImmutableSecret<>(key));
    }

    @Bean
    JwtDecoder jwtDecoder() {
        NimbusJwtDecoder decoder = NimbusJwtDecoder.withSecretKey(key).macAlgorithm(MacAlgorithm.HS256).build();
        decoder.setJwtValidator(JwtValidators.createDefaultWithIssuer(ISSUER));
        return decoder;
    }

    // 키가 없거나 짧으면 뜨지 않는다. 약한 키로 서명한 토큰이 나가는 것보다 낫다
    private static SecretKey decodeKey(String secret) {
        if (secret == null || secret.isBlank()) {
            throw new IllegalStateException("SIMAI_JWT_SECRET 이 비어 있다");
        }
        byte[] bytes;
        try {
            bytes = Base64.getDecoder().decode(secret.trim());
        } catch (IllegalArgumentException e) {
            throw new IllegalStateException("SIMAI_JWT_SECRET 은 Base64 여야 한다", e);
        }
        if (bytes.length < MIN_SECRET_BYTES) {
            throw new IllegalStateException("SIMAI_JWT_SECRET 은 " + MIN_SECRET_BYTES + "바이트 이상이어야 한다");
        }
        return new SecretKeySpec(bytes, "HmacSHA256");
    }
}
