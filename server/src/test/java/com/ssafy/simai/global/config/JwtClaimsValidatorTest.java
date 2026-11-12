package com.ssafy.simai.global.config;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.springframework.security.oauth2.jwt.Jwt;

/** 컨트롤러가 sub·role 을 바로 읽으므로 형식이 틀린 토큰은 해독기에서 거절한다(S15P31A101-632). */
class JwtClaimsValidatorTest {

    private static Jwt jwt(String subject, String role) {
        Jwt.Builder b = Jwt.withTokenValue("t").header("alg", "HS256").issuedAt(Instant.now())
                .expiresAt(Instant.now().plusSeconds(60)).claim("iss", JwtConfig.ISSUER);
        if (subject != null) {
            b.subject(subject);
        }
        if (role != null) {
            b.claim(AccessTokenIssuer.ROLE_CLAIM, role);
        }
        return b.build();
    }

    @Test
    void 계정_번호와_알려진_역할이면_받는다() {
        assertThat(JwtConfig.validateClaims(jwt("42", "ADMIN")).hasErrors()).isFalse();
        assertThat(JwtConfig.validateClaims(jwt("1", "VIEWER")).hasErrors()).isFalse();
    }

    @Test
    void sub_가_계정_번호가_아니면_거절한다() {
        assertThat(JwtConfig.validateClaims(jwt("not-a-number", "ADMIN")).hasErrors()).isTrue();
        assertThat(JwtConfig.validateClaims(jwt("0", "ADMIN")).hasErrors()).isTrue();
        assertThat(JwtConfig.validateClaims(jwt("-3", "ADMIN")).hasErrors()).isTrue();
        assertThat(JwtConfig.validateClaims(jwt("99999999999999999999", "ADMIN")).hasErrors()).isTrue();
        assertThat(JwtConfig.validateClaims(jwt(null, "ADMIN")).hasErrors()).isTrue();
    }

    @Test
    void role_이_없거나_모르는_값이면_거절한다() {
        assertThat(JwtConfig.validateClaims(jwt("42", null)).hasErrors()).isTrue();
        assertThat(JwtConfig.validateClaims(jwt("42", "ROOT")).hasErrors()).isTrue();
        assertThat(JwtConfig.validateClaims(jwt("42", "admin")).hasErrors()).isTrue();
    }
}
