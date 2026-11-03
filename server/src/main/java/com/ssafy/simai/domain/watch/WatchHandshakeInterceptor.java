package com.ssafy.simai.domain.watch;

import com.ssafy.simai.global.security.TokenHashes;
import java.security.Principal;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.server.ServerHttpRequest;
import org.springframework.http.server.ServerHttpResponse;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.WebSocketHandler;
import org.springframework.web.socket.server.HandshakeInterceptor;

/**
 * 워치 I4 업그레이드는 기기 토큰 체인(SecurityConfig deviceFilterChain: 토큰 없음·틀림·폐기는 401)을 지난 뒤, 그 기기가 워치(WATCH)일 때만
 * 받는다(노트북 토큰으로 워치 행세를 못 하게, CctvSourceHandshakeInterceptor 와 반대). 연결 중 폐기·회전을 하트비트마다 확인하려고 이 연결을
 * 연 토큰의 SHA-256 을 세션 속성에 둔다(원문은 두지 않는다). DB 를 못 읽으면 503(워치가 백오프로 다시 붙는다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WatchHandshakeInterceptor implements HandshakeInterceptor {

    static final String DEVICE_ID = "simai.watch.device";
    static final String TOKEN_HASH = "simai.watch.token-hash";

    private static final Logger log = LoggerFactory.getLogger(WatchHandshakeInterceptor.class);
    private static final String BEARER = "Bearer ";

    private final WatchRepository repository;

    WatchHandshakeInterceptor(WatchRepository repository) {
        this.repository = repository;
    }

    @Override
    public boolean beforeHandshake(ServerHttpRequest request, ServerHttpResponse response, WebSocketHandler wsHandler,
            Map<String, Object> attributes) {
        Principal principal = request.getPrincipal();
        String header = request.getHeaders().getFirst(HttpHeaders.AUTHORIZATION);
        if (principal == null || header == null || !header.regionMatches(true, 0, BEARER, 0, BEARER.length())) {
            response.setStatusCode(HttpStatus.UNAUTHORIZED);
            return false;
        }
        String kind;
        try {
            kind = repository.deviceKind(principal.getName());
        } catch (DataAccessException e) {
            log.warn("워치 기기를 확인하지 못했다: {}", e.getClass().getSimpleName());
            response.setStatusCode(HttpStatus.SERVICE_UNAVAILABLE);
            return false;
        }
        if (!"WATCH".equals(kind)) {
            response.setStatusCode(HttpStatus.FORBIDDEN);
            return false;
        }
        attributes.put(DEVICE_ID, principal.getName());
        attributes.put(TOKEN_HASH, TokenHashes.sha256Hex(header.substring(BEARER.length()).strip()));
        return true;
    }

    @Override
    public void afterHandshake(ServerHttpRequest request, ServerHttpResponse response, WebSocketHandler wsHandler,
            Exception exception) {
    }
}
