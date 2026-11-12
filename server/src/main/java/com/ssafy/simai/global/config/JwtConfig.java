package com.ssafy.simai.global.config;

import com.nimbusds.jose.jwk.source.ImmutableSecret;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.global.properties.AuthProperties;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.util.Arrays;
import java.util.Base64;
import java.util.regex.Pattern;
import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.oauth2.core.DelegatingOAuth2TokenValidator;
import org.springframework.security.oauth2.core.OAuth2Error;
import org.springframework.security.oauth2.core.OAuth2TokenValidatorResult;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.Jwt;
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
    /** app_user.id(bigint, 1 이상). long 에 들어가는 자릿수만 받는다 */
    private static final Pattern USER_ID = Pattern.compile("^[1-9][0-9]{0,17}$");

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
        decoder.setJwtValidator(new DelegatingOAuth2TokenValidator<>(
                JwtValidators.createDefaultWithIssuer(ISSUER), JwtConfig::validateClaims));
        return decoder;
    }

    /**
     * 컨트롤러가 Long.parseLong(sub)·Role.valueOf(role) 로 바로 읽는다. 형식이 틀린 토큰은 여기서 401 로 거절해
     * 엔드포인트마다 NumberFormatException·NullPointerException 이 500 으로 새지 않게 한다.
     */
    static OAuth2TokenValidatorResult validateClaims(Jwt jwt) {
        String subject = jwt.getSubject();
        if (subject == null || !USER_ID.matcher(subject).matches()) {
            return OAuth2TokenValidatorResult.failure(new OAuth2Error("invalid_token", "sub 가 계정 번호가 아니다", null));
        }
        String role = jwt.getClaimAsString(AccessTokenIssuer.ROLE_CLAIM);
        if (role == null || Arrays.stream(Role.values()).noneMatch(r -> r.name().equals(role))) {
            return OAuth2TokenValidatorResult.failure(new OAuth2Error("invalid_token", "role 이 없거나 알 수 없다", null));
        }
        return OAuth2TokenValidatorResult.success();
    }

    /** 키가 없거나 짧으면 뜨지 않는다. 약한 키로 서명한 토큰이 나가는 것보다 낫다. 구성 묶음 주소 서명 키도 이 키에서 뽑는다 */
    public static SecretKey decodeKey(String secret) {
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
