package com.ssafy.simai.global.security;

import com.ssafy.simai.domain.machine.MachineKind;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.exception.ErrorCode;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.List;
import java.util.Optional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpHeaders;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * 기기(/api/devices/**·GET /api/endpoints/tier0)·워커(/api/worker/**) 경로의 Authorization: Bearer 를 그 종류의 토큰으로 확인한다
 * (S15P31A101-222, 명세 14). 맞으면 주체 ID 를 principal(문자열)로, ROLE_DEVICE·ROLE_WORKER 로 인증하고, 아니면 아무것도 하지 않는다
 * (뒤의 authorizeHttpRequests 가 401). 폐기·회전된 토큰은 바로 거부된다(요청마다 DB 확인). 사용자 JWT 체인과 따로 둔다.
 * DB 를 못 읽으면 503 + ProblemDetail 로 끝낸다(토큰 거부 401 과 구분해 기기가 다시 시도하게).
 */
public class MachineTokenAuthenticationFilter extends OncePerRequestFilter {

    private static final Logger log = LoggerFactory.getLogger(MachineTokenAuthenticationFilter.class);
    private static final String BEARER = "Bearer ";

    private final MachineKind kind;
    private final MachineTokenService tokens;

    public MachineTokenAuthenticationFilter(MachineKind kind, MachineTokenService tokens) {
        this.kind = kind;
        this.tokens = tokens;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        String header = request.getHeader(HttpHeaders.AUTHORIZATION);
        // 같은 체인의 앞 필터(업로드 체인의 Tier 0 토큰)가 이미 인증했으면 DB 를 보지 않는다. DB 장애로 Tier 0 요청까지 503 이 되지 않게
        boolean authenticated = SecurityContextHolder.getContext().getAuthentication() != null;
        if (!authenticated && header != null && header.regionMatches(true, 0, BEARER, 0, BEARER.length())) {
            String token = header.substring(BEARER.length()).strip();
            // 기계 토큰은 점이 없는 Base64url 이다(TokenHashes.newToken). 점 두 개인 JWT 는 DB 를 보지 않고 JWT 해석에 넘긴다
            if (!token.isEmpty() && !looksLikeJwt(token)) {
                Optional<String> id;
                try {
                    id = tokens.authenticate(kind, token);
                } catch (DataAccessException e) {
                    log.warn("{} 토큰을 확인하지 못했다: {}", kind, e.getMessage());
                    SecurityErrorResponder.write(response, ErrorCode.Machine.TOKEN_CHECK_UNAVAILABLE);
                    return;
                }
                id.ifPresent(principal -> SecurityContextHolder.getContext().setAuthentication(
                        UsernamePasswordAuthenticationToken.authenticated(principal, null,
                                List.of(new SimpleGrantedAuthority(kind.authority())))));
            }
        }
        chain.doFilter(request, response);
    }

    private static boolean looksLikeJwt(String token) {
        return token.chars().filter(c -> c == '.').count() == 2;
    }
}
