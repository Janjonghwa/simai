package com.ssafy.simai.domain.watch;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

/**
 * 워치 ↔ EC2 I4 WebSocket(/ws/watch, 명세 부록 C.6·12.2 I4, 계약 watch_*, S15P31A101-336). 인증은 업그레이드 때 끝났다
 * (기기 토큰 체인 + WatchHandshakeInterceptor). 받은 것은 WatchHub 로 넘긴다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WatchWebSocketHandler extends TextWebSocketHandler {

    private final WatchHub hub;

    WatchWebSocketHandler(WatchHub hub) {
        this.hub = hub;
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) {
        hub.connected(session, (String) session.getAttributes().get(WatchHandshakeInterceptor.DEVICE_ID),
                (String) session.getAttributes().get(WatchHandshakeInterceptor.TOKEN_HASH));
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) {
        hub.received(session, message.getPayload());
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        hub.closed(session, status);
    }
}
