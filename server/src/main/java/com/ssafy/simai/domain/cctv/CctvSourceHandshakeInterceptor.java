package com.ssafy.simai.domain.cctv;

import java.security.Principal;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.http.server.ServerHttpRequest;
import org.springframework.http.server.ServerHttpResponse;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.WebSocketHandler;
import org.springframework.web.socket.server.HandshakeInterceptor;

/**
 * CCTV 수신 업그레이드는 기기 토큰 체인을 지난 뒤, 그 기기가 현장 노트북(LAPTOP)일 때만 받는다(워치 토큰으로 영상을 올리지 못하게,
 * S15P31A101-400). DB 를 못 읽으면 503 으로 끝낸다(중계가 백오프로 다시 붙는다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CctvSourceHandshakeInterceptor implements HandshakeInterceptor {

    private static final Logger log = LoggerFactory.getLogger(CctvSourceHandshakeInterceptor.class);

    private final CctvUsageRepository repository;

    public CctvSourceHandshakeInterceptor(CctvUsageRepository repository) {
        this.repository = repository;
    }

    @Override
    public boolean beforeHandshake(ServerHttpRequest request, ServerHttpResponse response, WebSocketHandler wsHandler,
            Map<String, Object> attributes) {
        Principal principal = request.getPrincipal();
        String kind;
        try {
            kind = principal == null ? null : repository.deviceKind(principal.getName());
        } catch (DataAccessException e) {
            log.warn("원격 CCTV 수신 기기를 확인하지 못했다: {}", e.getClass().getSimpleName());
            response.setStatusCode(HttpStatus.SERVICE_UNAVAILABLE);
            return false;
        }
        if (!"LAPTOP".equals(kind)) {
            response.setStatusCode(HttpStatus.FORBIDDEN);
            return false;
        }
        return true;
    }

    @Override
    public void afterHandshake(ServerHttpRequest request, ServerHttpResponse response, WebSocketHandler wsHandler,
            Exception exception) {
    }
}
