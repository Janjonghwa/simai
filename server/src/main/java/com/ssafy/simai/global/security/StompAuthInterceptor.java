package com.ssafy.simai.global.security;

import com.ssafy.simai.domain.realtime.RealtimeTopics;
import java.security.Principal;
import java.time.Instant;
import org.springframework.http.HttpHeaders;
import org.springframework.messaging.Message;
import org.springframework.messaging.MessageChannel;
import org.springframework.messaging.simp.stomp.StompCommand;
import org.springframework.messaging.simp.stomp.StompHeaderAccessor;
import org.springframework.messaging.support.ChannelInterceptor;
import org.springframework.messaging.support.MessageHeaderAccessor;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.security.authentication.AbstractAuthenticationToken;
import org.springframework.security.authentication.BadCredentialsException;
import org.springframework.security.core.Authentication;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtException;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationConverter;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;

/**
 * 관제 앱 STOMP(I2) 인증·권한(명세 14장: STOMP 토픽 조회는 열람 이상. 관리자 전용 토픽은 없다: 조치 불일치 /topic/admin/events 는 S15P31A101-411 로 지웠다).
 * 브라우저 WebSocket 은 업그레이드 요청에 Authorization 을 실을 수 없어 CONNECT 프레임의 Authorization 헤더(Bearer JWT)로 인증한다.
 * 토큰이 없거나 틀리면 예외를 던지고, Spring 이 ERROR 프레임을 보낸 뒤 연결을 닫는다(관제 앱은 새 토큰으로 다시 붙는다).
 * 구독(SUBSCRIBE)마다 CONNECT 때 받은 토큰의 만료를 다시 본다. 이미 구독한 토픽은 만료돼도 끊지 않는다
 * (관제 중 끊김을 만들지 않는다, 명세 10장). 관제 앱은 재연결마다 새 토큰을 싣는다.
 * 앱은 토픽을 구독만 한다. 클라이언트가 보내는 SEND 는 받지 않는다(명령은 REST, 명세 12.6).
 */
public class StompAuthInterceptor implements ChannelInterceptor {

    private static final String BEARER = "Bearer ";

    private final JwtDecoder decoder;
    private final JwtAuthenticationConverter converter;

    public StompAuthInterceptor(JwtDecoder decoder, JwtAuthenticationConverter converter) {
        this.decoder = decoder;
        this.converter = converter;
    }

    @Override
    public Message<?> preSend(Message<?> message, MessageChannel channel) {
        StompHeaderAccessor accessor = MessageHeaderAccessor.getAccessor(message, StompHeaderAccessor.class);
        if (accessor == null || accessor.getCommand() == null) {
            return message;
        }
        StompCommand command = accessor.getCommand();
        if (command == StompCommand.CONNECT || command == StompCommand.STOMP) {
            accessor.setUser(authenticate(accessor.getFirstNativeHeader(HttpHeaders.AUTHORIZATION)));
        } else if (command == StompCommand.SUBSCRIBE) {
            authorizeSubscribe(accessor.getUser(), accessor.getDestination());
        } else if (command == StompCommand.SEND) {
            throw new AccessDeniedException("관제 앱은 STOMP 로 보내지 않는다");
        }
        return message;
    }

    private Authentication authenticate(String header) {
        if (header == null || !header.startsWith(BEARER)) {
            throw new BadCredentialsException("CONNECT 에 Bearer 토큰이 없다");
        }
        try {
            AbstractAuthenticationToken authentication = converter.convert(decoder.decode(header.substring(BEARER.length()).trim()));
            if (authentication == null) {
                throw new BadCredentialsException("토큰에서 계정을 읽지 못했다");
            }
            return authentication;
        } catch (JwtException e) {
            throw new BadCredentialsException("토큰이 유효하지 않다", e);
        }
    }

    private static void authorizeSubscribe(Principal user, String destination) {
        if (!(user instanceof Authentication authentication) || !authentication.isAuthenticated()) {
            throw new AccessDeniedException("인증되지 않은 구독");
        }
        if (authentication instanceof JwtAuthenticationToken jwt && jwt.getToken().getExpiresAt() != null
                && !jwt.getToken().getExpiresAt().isAfter(Instant.now())) {
            throw new AccessDeniedException("토큰이 만료된 연결의 구독");
        }
        if (destination != null && RealtimeTopics.VIEWER.contains(destination)) {
            return;
        }
        throw new AccessDeniedException("구독할 수 없는 토픽: " + destination);
    }
}
