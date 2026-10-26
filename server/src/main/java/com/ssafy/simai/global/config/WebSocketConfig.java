package com.ssafy.simai.global.config;

import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.domain.tier0.i1.Tier0I1WebSocketHandler;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.socket.config.annotation.EnableWebSocket;
import org.springframework.web.socket.config.annotation.WebSocketConfigurer;
import org.springframework.web.socket.config.annotation.WebSocketHandlerRegistry;

/**
 * 기기·서비스가 붙는 WebSocket(STOMP 아님). 지금은 I1(Tier 0, /ws/tier0) 하나다.
 * Tier 0 는 브라우저가 아니라 Origin 을 싣지 않는다. Origin 이 없는 요청은 출처 검사를 받지 않는다.
 */
@Configuration
@EnableWebSocket
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WebSocketConfig implements WebSocketConfigurer {

    private final Tier0I1WebSocketHandler tier0Handler;

    WebSocketConfig(Tier0I1WebSocketHandler tier0Handler) {
        this.tier0Handler = tier0Handler;
    }

    @Override
    public void registerWebSocketHandlers(WebSocketHandlerRegistry registry) {
        registry.addHandler(tier0Handler, Tier0Paths.I1);
    }
}
