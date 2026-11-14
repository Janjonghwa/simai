package com.ssafy.simai.global.security;

import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.properties.Tier0Properties;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpHeaders;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * I1(/ws/tier0) 업그레이드 요청의 Authorization: Bearer 를 Tier 0 토큰 해시와 비교한다(Tier0Properties).
 * 맞으면 ROLE_TIER0 로 인증하고, 아니면 아무것도 하지 않는다(뒤의 authorizeHttpRequests 가 401 을 낸다).
 * 사용자 JWT 체인과 따로 둔다. 같은 체인이면 JWT 리소스 서버가 Tier 0 토큰을 JWT 로 읽다가 거부한다.
 * 설정 값이 64자리 16진수가 아니면(sha256sum 출력 줄을 통째로 넣는 등) 경고만 남기고 환경 변수 토큰을 모두 거부한다. 서버는 뜬다.
 * GPU 세션이 발급한 Tier 0 토큰도 받는다(S15P31A101-699, Tier0SessionTokens): 환경 변수 해시와 다르면 열린 세션의 토큰인지 DB 로 본다.
 * 그때 principal 이름은 {@value #SESSION_PRINCIPAL_PREFIX}&lt;세션 ID&gt; 라 세션이 끝나면 그 I1 연결을 찾아 끊는다(GpuSessionSweeper).
 * DB 를 못 읽으면 503 으로 끝낸다(토큰 거부 401 과 구분해 Tier 0 가 다시 시도하게). 환경 변수 토큰은 DB 를 보지 않는다.
 */
public class Tier0TokenAuthenticationFilter extends OncePerRequestFilter {

    public static final String ROLE = "ROLE_TIER0";
    /** GPU 세션 토큰으로 인증한 Tier 0 의 principal 이름 접두(뒤에 세션 UUID) */
    public static final String SESSION_PRINCIPAL_PREFIX = "gpu-session:";
    private static final String BEARER = "Bearer ";
    private static final Pattern SHA256_HEX = Pattern.compile("^[0-9a-f]{64}$");
    private static final Logger log = LoggerFactory.getLogger(Tier0TokenAuthenticationFilter.class);

    private final byte[] expectedHash;
    private final Tier0SessionTokens sessions;

    public Tier0TokenAuthenticationFilter(Tier0Properties properties) {
        this(properties, null);
    }

    /** @param sessions GPU 세션 토큰 확인. null 이면 환경 변수 토큰만 받는다 */
    public Tier0TokenAuthenticationFilter(Tier0Properties properties, Tier0SessionTokens sessions) {
        this.expectedHash = expectedHash(properties.tokenSha256());
        this.sessions = sessions;
    }

    private static byte[] expectedHash(String configured) {
        if (configured.isEmpty()) {
            log.warn("simai.tier0.token-sha256(SIMAI_TIER0_TOKEN_SHA256) 이 비어 환경 변수 Tier 0 토큰을 받지 않는다(GPU 세션 토큰만)");
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
        if (header != null && header.regionMatches(true, 0, BEARER, 0, BEARER.length())) {
            String token = header.substring(BEARER.length()).strip();
            byte[] actual = TokenHashes.sha256(token);
            // 걸린 시간으로 몇 바이트가 맞았는지 드러나지 않게 고정 시간 비교를 쓴다
            if (expectedHash != null && MessageDigest.isEqual(expectedHash, actual)) {
                authenticate("tier0");
            } else if (sessions != null && !token.isEmpty() && !looksLikeJwt(token)) {
                // 해시로 찾으므로(UNIQUE 인덱스) 비교 시간이 토큰 내용에 따라 달라지지 않는다
                Optional<UUID> session;
                try {
                    session = sessions.activeSessionForTier0Token(HexFormat.of().formatHex(actual));
                } catch (DataAccessException e) {
                    log.warn("GPU 세션 Tier 0 토큰을 확인하지 못했다: {}", e.getMessage());
                    SecurityErrorResponder.write(response, ErrorCode.Machine.TOKEN_CHECK_UNAVAILABLE);
                    return;
                }
                session.ifPresent(id -> authenticate(SESSION_PRINCIPAL_PREFIX + id));
            }
        }
        chain.doFilter(request, response);
    }

    private static void authenticate(String principal) {
        SecurityContextHolder.getContext().setAuthentication(UsernamePasswordAuthenticationToken.authenticated(
                principal, null, List.of(new SimpleGrantedAuthority(ROLE))));
    }

    /** 사용자 JWT(점 두 개)는 DB 를 보지 않는다(업로드 체인에서 JWT 해석으로 넘어간다) */
    private static boolean looksLikeJwt(String token) {
        return token.chars().filter(c -> c == '.').count() == 2;
    }
}
