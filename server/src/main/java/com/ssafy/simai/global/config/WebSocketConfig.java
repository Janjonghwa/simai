package com.ssafy.simai.global.config;

import com.ssafy.simai.domain.avatar.AvatarPaths;
import com.ssafy.simai.domain.avatar.AvatarWebSocketHandler;
import com.ssafy.simai.domain.cctv.CctvPaths;
import com.ssafy.simai.domain.cctv.CctvSourceHandshakeInterceptor;
import com.ssafy.simai.domain.cctv.CctvSourceWebSocketHandler;
import com.ssafy.simai.domain.cctv.CctvTicketHandshakeInterceptor;
import com.ssafy.simai.domain.cctv.CctvViewWebSocketHandler;
import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.domain.tier0.i1.Tier0I1WebSocketHandler;
import com.ssafy.simai.domain.watch.WatchHandshakeInterceptor;
import com.ssafy.simai.domain.watch.WatchPaths;
import com.ssafy.simai.domain.watch.WatchWebSocketHandler;
import com.ssafy.simai.global.properties.AuthProperties;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.socket.config.annotation.EnableWebSocket;
import org.springframework.web.socket.config.annotation.WebSocketConfigurer;
import org.springframework.web.socket.config.annotation.WebSocketHandlerRegistry;

/**
 * STOMP 가 아닌 WebSocket. I1(Tier 0, /ws/tier0), I6(분석 워커 아바타 스트림, /ws/worker/avatar), CCTV 원격 시청 수신
 * (게이트웨이 영상 중계, /ws/devices/cctv)과 시청(관리자 브라우저, /ws/cctv/view, S15P31A101-400), 워치 I4(/ws/watch, S15P31A101-336)다.
 * 관제 앱 STOMP(/ws)는 StompConfig. Tier 0·워커·중계·워치는 브라우저가 아니라 Origin 을 싣지 않는다. Origin 이 없는 요청은 출처 검사를 받지 않는다.
 * 시청은 브라우저라 STOMP 와 같은 출처 목록(simai.auth.cors-allowed-origins, 비면 같은 출처만)을 쓴다.
 */
@Configuration
@EnableWebSocket
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WebSocketConfig implements WebSocketConfigurer {

    private final Tier0I1WebSocketHandler tier0Handler;
    private final AvatarWebSocketHandler avatarHandler;
    private final CctvSourceWebSocketHandler cctvSourceHandler;
    private final CctvSourceHandshakeInterceptor cctvSourceInterceptor;
    private final CctvViewWebSocketHandler cctvViewHandler;
    private final CctvTicketHandshakeInterceptor cctvTicketInterceptor;
    private final WatchWebSocketHandler watchHandler;
    private final WatchHandshakeInterceptor watchInterceptor;
    private final AuthProperties auth;

    WebSocketConfig(Tier0I1WebSocketHandler tier0Handler, AvatarWebSocketHandler avatarHandler,
            CctvSourceWebSocketHandler cctvSourceHandler, CctvSourceHandshakeInterceptor cctvSourceInterceptor,
            CctvViewWebSocketHandler cctvViewHandler, CctvTicketHandshakeInterceptor cctvTicketInterceptor,
            WatchWebSocketHandler watchHandler, WatchHandshakeInterceptor watchInterceptor, AuthProperties auth) {
        this.tier0Handler = tier0Handler;
        this.avatarHandler = avatarHandler;
        this.cctvSourceHandler = cctvSourceHandler;
        this.cctvSourceInterceptor = cctvSourceInterceptor;
        this.cctvViewHandler = cctvViewHandler;
        this.cctvTicketInterceptor = cctvTicketInterceptor;
        this.watchHandler = watchHandler;
        this.watchInterceptor = watchInterceptor;
        this.auth = auth;
    }

    @Override
    public void registerWebSocketHandlers(WebSocketHandlerRegistry registry) {
        registry.addHandler(tier0Handler, Tier0Paths.I1);
        registry.addHandler(avatarHandler, AvatarPaths.WORKER_WS);
        registry.addHandler(cctvSourceHandler, CctvPaths.SOURCE_WS).addInterceptors(cctvSourceInterceptor);
        registry.addHandler(watchHandler, WatchPaths.WS).addInterceptors(watchInterceptor);
        registry.addHandler(cctvViewHandler, CctvPaths.VIEW_WS)
                .addInterceptors(cctvTicketInterceptor)
                .setAllowedOrigins(auth.corsAllowedOrigins().toArray(String[]::new));
    }
}
