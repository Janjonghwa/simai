package com.ssafy.simai.global.security;

import com.ssafy.simai.domain.machine.MachineKind;
import com.ssafy.simai.domain.machine.MachineTokenService;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.List;
import org.springframework.http.HttpHeaders;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * 기기(/api/devices/**·/api/endpoints/tier0)·워커(/api/worker/**) 경로의 Authorization: Bearer 를 그 종류의 토큰으로 확인한다
 * (S15P31A101-222, 명세 14). 맞으면 주체 ID 를 principal(문자열)로, ROLE_DEVICE·ROLE_WORKER 로 인증하고, 아니면 아무것도 하지 않는다
 * (뒤의 authorizeHttpRequests 가 401). 폐기·회전된 토큰은 바로 거부된다(요청마다 DB 확인). 사용자 JWT 체인과 따로 둔다.
 */
public class MachineTokenAuthenticationFilter extends OncePerRequestFilter {

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
        if (header != null && header.regionMatches(true, 0, BEARER, 0, BEARER.length())) {
            String token = header.substring(BEARER.length()).strip();
            if (!token.isEmpty()) {
                tokens.authenticate(kind, token).ifPresent(id -> SecurityContextHolder.getContext().setAuthentication(
                        UsernamePasswordAuthenticationToken.authenticated(id, null,
                                List.of(new SimpleGrantedAuthority(kind.authority())))));
            }
        }
        chain.doFilter(request, response);
    }
}
