package com.ssafy.simai.domain.event;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.util.List;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * 명세 12.6 /api/events(반장·관리자). 확인 본문 {event_sequence?, channel?(CONTROL_ROOM 기본·FOREMAN_PWA)}.
 * 항목은 계약 risk_event payload 와 같은 snake_case 이고 acks[] 를 싣는다(관제 앱 types.ts).
 * 같은 사람의 여러 규칙을 한 묶음으로 보이는 일은 화면이 한다(7장·13장 "화면에서", 관제 앱 groupIncidents). 서버는 이벤트를 하나씩 준다.
 * 실시간 갱신은 /topic/events(S15P31A101-119).
 */
@RestController
@RequestMapping("/api/events")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventController {

    static final int DEFAULT_SIZE = 20;
    static final int MAX_SIZE = 100;

    private final EventQueryRepository events;
    private final EventAckService acks;

    EventController(EventQueryRepository events, EventAckService acks) {
        this.events = events;
        this.acks = acks;
    }

    /** Spring Page 와 같은 모양. 관제 앱은 content 만 읽는다 */
    public record EventPage(List<ObjectNode> content, long totalElements, int number, int size) {
    }

    /** 확인 본문. event_sequence 는 화면이 본 sequence(없으면 EC2 가 마지막 sequence 로 채운다) */
    public record AckRequest(@JsonProperty("event_sequence") Integer eventSequence, @JsonProperty("channel") String channel) {
    }

    @GetMapping
    @PreAuthorize("hasAnyRole('FOREMAN', 'ADMIN')")
    EventPage list(@RequestParam(defaultValue = "0") int page, @RequestParam(defaultValue = "" + DEFAULT_SIZE) int size) {
        int safePage = Math.max(page, 0);
        int safeSize = Math.clamp(size, 1, MAX_SIZE);
        return new EventPage(events.findPage(safePage, safeSize), events.count(), safePage, safeSize);
    }

    @PostMapping("/{eventId}/ack")
    @PreAuthorize("hasAnyRole('FOREMAN', 'ADMIN')")
    ObjectNode ack(@PathVariable UUID eventId, @RequestBody(required = false) AckRequest request, @AuthenticationPrincipal Jwt jwt) {
        Integer seen = request == null ? null : request.eventSequence();
        String channel = request == null ? null : request.channel();
        Role role = Role.valueOf(jwt.getClaimAsString(AccessTokenIssuer.ROLE_CLAIM));
        return acks.acknowledge(eventId, seen, channel, Long.parseLong(jwt.getSubject()), role);
    }
}
