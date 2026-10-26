package com.ssafy.simai.global.security;

import com.ssafy.simai.global.properties.Tier0Properties;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.List;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * I1(/ws/tier0) 업그레이드 요청의 Authorization: Bearer 를 Tier 0 토큰 해시와 비교한다(Tier0Properties).
 * 맞으면 ROLE_TIER0 로 인증하고, 아니면 아무것도 하지 않는다(뒤의 authorizeHttpRequests 가 401 을 낸다).
 * 사용자 JWT 체인과 따로 둔다. 같은 체인이면 JWT 리소스 서버가 Tier 0 토큰을 JWT 로 읽다가 거부한다.
 * 설정 값이 64자리 16진수가 아니면(sha256sum 출력 줄을 통째로 넣는 등) 경고만 남기고 I1 을 모두 거부한다. 서버는 뜬다.
 */
public class Tier0TokenAuthenticationFilter extends OncePerRequestFilter {

    public static final String ROLE = "ROLE_TIER0";
    private static final String BEARER = "Bearer ";
    private static final Pattern SHA256_HEX = Pattern.compile("^[0-9a-f]{64}$");
    private static final Logger log = LoggerFactory.getLogger(Tier0TokenAuthenticationFilter.class);

    private final byte[] expectedHash;

    public Tier0TokenAuthenticationFilter(Tier0Properties properties) {
        this.expectedHash = expectedHash(properties.tokenSha256());
    }

    private static byte[] expectedHash(String configured) {
        if (configured.isEmpty()) {
            log.warn("simai.tier0.token-sha256(SIMAI_TIER0_TOKEN_SHA256) 이 비어 I1(/ws/tier0) 연결을 모두 거부한다");
            return null;
        }
        if (!SHA256_HEX.matcher(configured).matches()) {
            // 값은 로그에 남기지 않는다. 길이만 알려 잘못 붙여 넣은 것을 찾게 한다
            log.warn("simai.tier0.token-sha256 이 SHA-256 16진수 64자리가 아니라(길이 {}) I1 연결을 모두 거부한다", configured.length());
            return null;
        }
        return HexFormat.of().parseHex(configured);
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        String header = request.getHeader(HttpHeaders.AUTHORIZATION);
        // 인증 방식 이름은 대소문자를 가리지 않는다(RFC 7235)
        if (expectedHash != null && header != null && header.regionMatches(true, 0, BEARER, 0, BEARER.length())) {
            byte[] actual = TokenHashes.sha256(header.substring(BEARER.length()).strip());
            // 걸린 시간으로 몇 바이트가 맞았는지 드러나지 않게 고정 시간 비교를 쓴다
            if (MessageDigest.isEqual(expectedHash, actual)) {
                SecurityContextHolder.getContext().setAuthentication(UsernamePasswordAuthenticationToken.authenticated(
                        "tier0", null, List.of(new SimpleGrantedAuthority(ROLE))));
            }
        }
        chain.doFilter(request, response);
    }
}
