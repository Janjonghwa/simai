package com.ssafy.simai.domain.avatar;

import java.security.Principal;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

/**
 * I6 GPU 분석 워커 → EC2 아바타 스트림 WebSocket(/ws/worker/avatar, 워커 토큰 인증, S15P31A101-325).
 * 업그레이드 인증은 SecurityConfig 의 workerFilterChain 에서 끝난다.
 * 메시지 하나가 계약 위반이거나 512 KiB 를 넘어도 연결은 끊지 않고 그 메시지만 버리고 센다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class AvatarWebSocketHandler extends TextWebSocketHandler {

    private static final Logger log = LoggerFactory.getLogger(AvatarWebSocketHandler.class);

    /**
     * 컨테이너 수신 한도는 1 MiB 로 열어 두고, 512 KiB 초과 본문은 핸들러에서 버리고 집계한다(1009 연결 종료 방지).
     */
    static final int CONTAINER_TEXT_BUFFER_BYTES = 1024 * 1024;

    private final AvatarStreamService stream;

    public AvatarWebSocketHandler(AvatarStreamService stream) {
        this.stream = stream;
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) {
        session.setTextMessageSizeLimit(CONTAINER_TEXT_BUFFER_BYTES);
        log.info("I6 아바타 스트림 연결됨: session={} worker={}", session.getId(), workerId(session));
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) {
        stream.handleRawMessage(message.getPayload(), workerId(session));
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        log.info("I6 아바타 스트림 연결 끊김: session={} worker={} status={}", session.getId(), workerId(session), status);
    }

    private static String workerId(WebSocketSession session) {
        Principal principal = session.getPrincipal();
        return principal == null ? null : principal.getName();
    }
}
