package com.ssafy.simai.domain.cctv;

import java.security.Principal;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.BinaryMessage;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.PongMessage;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.AbstractWebSocketHandler;

/**
 * 게이트웨이 영상 중계 → EC2 CCTV 수신 WebSocket(/ws/devices/cctv, S15P31A101-400). 업그레이드 인증은 SecurityConfig 의
 * deviceFilterChain(기기 토큰), 현장 노트북(LAPTOP)인지는 CctvSourceHandshakeInterceptor 가 본다. 받은 것은 CctvRemoteHub 로 넘긴다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CctvSourceWebSocketHandler extends AbstractWebSocketHandler {

    private final CctvRemoteHub hub;

    public CctvSourceWebSocketHandler(CctvRemoteHub hub) {
        this.hub = hub;
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) {
        Principal principal = session.getPrincipal();
        hub.sourceConnected(session, principal == null ? null : principal.getName());
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) {
        hub.sourceText(session, message.getPayload());
    }

    @Override
    protected void handleBinaryMessage(WebSocketSession session, BinaryMessage message) {
        hub.sourceBinary(session, message.getPayload());
    }

    @Override
    protected void handlePongMessage(WebSocketSession session, PongMessage message) {
        hub.pong(session);
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        hub.sourceClosed(session);
    }
}
