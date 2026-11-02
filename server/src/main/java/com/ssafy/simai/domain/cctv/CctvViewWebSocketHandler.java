package com.ssafy.simai.domain.cctv;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.PongMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.AbstractWebSocketHandler;

/**
 * EC2 → 관리자 브라우저 CCTV 시청 WebSocket(/ws/cctv/view?ticket=…, S15P31A101-400). 입장권은 CctvTicketHandshakeInterceptor 가
 * 업그레이드 전에 한 번 쓰고 사용자 ID 를 속성으로 넘긴다. 브라우저가 보내는 메시지는 쓰지 않는다(받는 쪽 상한 4 KiB).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CctvViewWebSocketHandler extends AbstractWebSocketHandler {

    private final CctvRemoteHub hub;

    public CctvViewWebSocketHandler(CctvRemoteHub hub) {
        this.hub = hub;
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) {
        Object userId = session.getAttributes().get(CctvTicketHandshakeInterceptor.USER_ID);
        hub.viewerConnected(session, userId instanceof Long id ? id : -1L);
    }

    @Override
    protected void handlePongMessage(WebSocketSession session, PongMessage message) {
        hub.pong(session);
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        hub.viewerClosed(session);
    }
}
