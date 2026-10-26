package com.ssafy.simai.global.config;

import com.ssafy.simai.domain.realtime.RealtimeTopics;
import com.ssafy.simai.global.properties.AuthProperties;
import com.ssafy.simai.global.security.StompAuthInterceptor;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Lazy;
import org.springframework.messaging.simp.config.ChannelRegistration;
import org.springframework.messaging.simp.config.MessageBrokerRegistry;
import org.springframework.scheduling.TaskScheduler;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationConverter;
import org.springframework.web.socket.config.annotation.EnableWebSocketMessageBroker;
import org.springframework.web.socket.config.annotation.StompEndpointRegistry;
import org.springframework.web.socket.config.annotation.WebSocketMessageBrokerConfigurer;
import org.springframework.web.socket.config.annotation.WebSocketTransportRegistration;

/**
 * 관제 앱 실시간 구간 I2(EC2 → 뷰어, STOMP over WebSocket, 명세 12.2·12.4). 엔드포인트 /ws, 토픽은 메모리 브로커(/topic)다.
 * 하트비트는 양쪽 10초로 관제 앱(front/apps/admin/src/lib/stomp.ts)과 같다. 끊김은 하트비트로 드러난다(명세 6장, 13.4).
 * 출처는 CORS 와 같은 목록(simai.auth.cors-allowed-origins)이고, 비어 있으면 같은 출처만 받는다.
 */
@Configuration
@EnableWebSocketMessageBroker
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class StompConfig implements WebSocketMessageBrokerConfigurer {

    private static final long HEARTBEAT_MS = 10_000;
    // 앱은 CONNECT·SUBSCRIBE 같은 작은 프레임만 보낸다
    private static final int INBOUND_MESSAGE_LIMIT_BYTES = 16 * 1024;

    private final AuthProperties auth;
    private final JwtDecoder jwtDecoder;
    private final JwtAuthenticationConverter jwtConverter;
    private TaskScheduler heartbeatScheduler;

    StompConfig(AuthProperties auth, JwtDecoder jwtDecoder, JwtAuthenticationConverter jwtConverter) {
        this.auth = auth;
        this.jwtDecoder = jwtDecoder;
        this.jwtConverter = jwtConverter;
    }

    // 브로커 설정이 만드는 스케줄러를 하트비트에 쓴다. 이 설정이 그 빈을 만들므로 늦게 받는다
    @Autowired
    void setHeartbeatScheduler(@Lazy @Qualifier("messageBrokerTaskScheduler") TaskScheduler scheduler) {
        this.heartbeatScheduler = scheduler;
    }

    @Override
    public void registerStompEndpoints(StompEndpointRegistry registry) {
        registry.addEndpoint(RealtimeTopics.ENDPOINT)
                .setAllowedOrigins(auth.corsAllowedOrigins().toArray(String[]::new));
    }

    @Override
    public void configureMessageBroker(MessageBrokerRegistry registry) {
        registry.enableSimpleBroker(RealtimeTopics.PREFIX)
                .setHeartbeatValue(new long[] {HEARTBEAT_MS, HEARTBEAT_MS})
                .setTaskScheduler(heartbeatScheduler);
    }

    @Override
    public void configureClientInboundChannel(ChannelRegistration registration) {
        registration.interceptors(new StompAuthInterceptor(jwtDecoder, jwtConverter));
    }

    @Override
    public void configureWebSocketTransport(WebSocketTransportRegistration registration) {
        registration.setMessageSizeLimit(INBOUND_MESSAGE_LIMIT_BYTES);
    }
}
