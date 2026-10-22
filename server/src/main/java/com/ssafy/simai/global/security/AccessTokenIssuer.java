package com.ssafy.simai.global.security;

import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.properties.AuthProperties;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.stereotype.Component;

/** 계정에 access JWT 를 발급한다. sub 는 user_id, role 클레임은 역할 하나다. */
@Component
public class AccessTokenIssuer {

    public static final String ROLE_CLAIM = "role";

    private final JwtEncoder encoder;
    private final Duration ttl;
    private final Clock clock;

    AccessTokenIssuer(JwtEncoder encoder, AuthProperties properties, Clock clock) {
        this.encoder = encoder;
        this.ttl = properties.jwt().accessTokenTtl();
        this.clock = clock;
    }

    public IssuedToken issue(AppUser user) {
        Instant now = clock.instant();
        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer(JwtConfig.ISSUER)
                .subject(String.valueOf(user.getId()))
                .issuedAt(now)
                .expiresAt(now.plus(ttl))
                .claim(ROLE_CLAIM, user.getRole().name())
                .build();
        JwsHeader header = JwsHeader.with(MacAlgorithm.HS256).build();
        String token = encoder.encode(JwtEncoderParameters.from(header, claims)).getTokenValue();
        return new IssuedToken(token, ttl);
    }

    public record IssuedToken(String value, Duration ttl) {
    }
}
