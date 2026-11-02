package com.ssafy.simai.domain.cctv;

import java.util.Map;
import java.util.Optional;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.server.ServerHttpRequest;
import org.springframework.http.server.ServerHttpResponse;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.WebSocketHandler;
import org.springframework.web.socket.server.HandshakeInterceptor;
import org.springframework.web.util.UriComponentsBuilder;

/**
 * 시청 WebSocket 업그레이드 전에 ?ticket= 입장권을 한 번 쓴다(S15P31A101-400). 없거나 지났거나 이미 썼으면 401 로 업그레이드하지 않는다.
 * 입장권은 관리자만 받으므로(POST /api/cctv/remote/tickets) 여기를 지나면 관리자다. 입장권 값은 로그에 남기지 않는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CctvTicketHandshakeInterceptor implements HandshakeInterceptor {

    static final String USER_ID = CctvTicketHandshakeInterceptor.class.getName() + ".userId";

    private final CctvTicketService tickets;

    public CctvTicketHandshakeInterceptor(CctvTicketService tickets) {
        this.tickets = tickets;
    }

    @Override
    public boolean beforeHandshake(ServerHttpRequest request, ServerHttpResponse response, WebSocketHandler wsHandler,
            Map<String, Object> attributes) {
        String ticket = UriComponentsBuilder.fromUri(request.getURI()).build().getQueryParams().getFirst("ticket");
        Optional<Long> userId = tickets.consume(ticket);
        if (userId.isEmpty()) {
            response.setStatusCode(HttpStatus.UNAUTHORIZED);
            return false;
        }
        attributes.put(USER_ID, userId.get());
        return true;
    }

    @Override
    public void afterHandshake(ServerHttpRequest request, ServerHttpResponse response, WebSocketHandler wsHandler,
            Exception exception) {
    }
}
