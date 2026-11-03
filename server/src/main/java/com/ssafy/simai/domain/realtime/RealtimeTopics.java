package com.ssafy.simai.domain.realtime;

import java.util.Set;

/** I2 STOMP 엔드포인트와 토픽(명세 12.2·12.4). 관제 앱 VITE_STOMP_URL 이 이 경로를 가리킨다. */
public final class RealtimeTopics {

    /** 관제 앱이 붙는 STOMP over WebSocket 경로(SockJS 없음). Nginx 의 location /ws 가 그대로 서버로 넘긴다. */
    public static final String ENDPOINT = "/ws";

    public static final String PREFIX = "/topic";
    public static final String WORLD = "/topic/world";
    public static final String EVENTS = "/topic/events";
    public static final String HEALTH = "/topic/health";
    public static final String AVATAR = "/topic/avatar";

    /** 열람 이상(로그인한 계정 모두)이 구독할 수 있는 토픽(명세 14장). 이 밖의 토픽은 누구도 구독하지 못한다. */
    public static final Set<String> VIEWER = Set.of(WORLD, EVENTS, HEALTH, AVATAR);

    private RealtimeTopics() {
    }
}
