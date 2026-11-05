package com.ssafy.simai.domain.watch;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

/**
 * 워치 수동 배정 API(잇기·풀기는 관리자만, 조회는 열람 이상, S15P31A101-338·-342). 명세 밖 임시안이라 계약(contracts/)에 두지 않는다: 시연 경로를 먼저 열고 -340 이
 * 움직임 대조로 바꾼다. 이름 없이 익명 World ID ↔ 워치 기기 ID 만 다룬다.
 * <ul>
 *   <li>GET /api/watch-bindings: 지금 배정과 채널 상태(BOUND 워치 연결 중, LOST 끊김)
 *   <li>PUT /api/watch-bindings/{deviceId} {"world_id"}: 잇기(그 워치·World ID 의 옛 배정은 끝냄)
 *   <li>DELETE /api/watch-bindings/{deviceId}: 풀기
 * </ul>
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class WatchBindingController {

    static final String API = "/api/watch-bindings";

    record BindRequest(@JsonProperty("world_id") String worldId) {
    }

    record BindingResponse(@JsonProperty("binding_id") long bindingId, @JsonProperty("device_id") String deviceId,
            @JsonProperty("world_id") String worldId, @JsonProperty("method") String method,
            @JsonProperty("bound_at") long boundAt, @JsonProperty("channel_state") String channelState) {
    }

    private final WatchBindings bindings;
    private final WatchHub hub;

    WatchBindingController(WatchBindings bindings, WatchHub hub) {
        this.bindings = bindings;
        this.hub = hub;
    }

    /** 조회는 열람 이상(14장 조회 권한): 관제 화면 이름표·워치 패널이 쓴다. 익명 World ID·기기 ID 만 싣는다(-342) */
    @GetMapping(API)
    List<BindingResponse> list() {
        return bindings.active().stream().map(this::response).toList();
    }

    @PutMapping(API + "/{deviceId}")
    @PreAuthorize("hasRole('ADMIN')")
    BindingResponse bind(@PathVariable String deviceId, @RequestBody BindRequest request, @AuthenticationPrincipal Jwt jwt) {
        return response(bindings.bindManually(deviceId, request == null ? null : request.worldId(), Long.parseLong(jwt.getSubject())));
    }

    @DeleteMapping(API + "/{deviceId}")
    @PreAuthorize("hasRole('ADMIN')")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void release(@PathVariable String deviceId) {
        bindings.release(deviceId);
    }

    private BindingResponse response(WatchBindings.Binding b) {
        return new BindingResponse(b.bindingId(), b.deviceId(), b.worldId(), b.method(), b.boundAt().toEpochMilli(),
                hub.isConnected(b.deviceId()) ? "BOUND" : "LOST");
    }
}
