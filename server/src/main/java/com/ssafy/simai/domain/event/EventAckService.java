package com.ssafy.simai.domain.event;

import com.ssafy.simai.domain.tier0.event.EventAckForwarder;
import com.ssafy.simai.domain.tier0.event.RiskEventsDelivered;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.stereotype.Service;
import tools.jackson.databind.node.ObjectNode;

/**
 * 운영자 확인(ACK, 명세 3.2·3.10, FR-9·39). 확인은 해소가 아니다: 노트북 반복 소리만 멈추고 표시는 해소될 때까지 남는다.
 * 확인 대상은 화면이 본 event_sequence 다. 없으면 EC2 가 받은 마지막 전이의 sequence 로 채운다(3.10). 그보다 뒤에 등급이 오르거나
 * 재개된 경보는 이 확인으로 멈추지 않는다(Tier 0 가 판단). sequence 는 EC2 가 올리지 않는다(Tier 0 의 ACKNOWLEDGED 전이가 올린다).
 * 해소됐거나 없는 이벤트는 Tier 0 가 응답 없이 버리므로 미리 막는다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventAckService {

    private static final Logger log = LoggerFactory.getLogger(EventAckService.class);

    private final EventQueryRepository events;
    private final EventAckForwarder forwarder;
    private final Clock clock;
    private final ApplicationEventPublisher publisher;

    public EventAckService(EventQueryRepository events, EventAckForwarder forwarder, Clock clock,
            ApplicationEventPublisher publisher) {
        this.events = events;
        this.forwarder = forwarder;
        this.clock = clock;
        this.publisher = publisher;
    }

    /**
     * @return 새 확인을 붙인 이벤트 항목(관제 앱이 바로 반영한다).
     * channel 은 확인한 화면(관제 화면 CONTROL_ROOM, 반장 PWA FOREMAN_PWA). 없으면 관제 화면이다.
     */
    public ObjectNode acknowledge(UUID eventId, Integer seenSequence, String requestedChannel, long userId, Role role) {
        if (seenSequence != null && seenSequence < 1) {
            throw new BusinessException(ErrorCode.Event.INVALID_EVENT_SEQUENCE);
        }
        ObjectNode event = events.findOne(eventId).orElseThrow(() -> new BusinessException(ErrorCode.Event.EVENT_NOT_FOUND));
        if ("RESOLVED".equals(event.path("lifecycle").asString())) {
            throw new BusinessException(ErrorCode.Event.EVENT_RESOLVED);
        }
        Integer current = event.has("event_sequence") ? event.get("event_sequence").intValue() : null;
        // 화면이 EC2 보다 앞선 sequence 를 말할 수는 없다. 넘치면 Tier 0 도 지금 sequence 로 줄인다(3.10)
        Integer target = seenSequence == null ? current : current == null ? seenSequence : Math.min(seenSequence, current);
        String channel = requestedChannel == null ? "CONTROL_ROOM" : requestedChannel;
        if (!channel.equals("CONTROL_ROOM") && !channel.equals("FOREMAN_PWA")) {
            throw new BusinessException(ErrorCode.Event.INVALID_ACK_CHANNEL);
        }
        UUID ackId = UUID.randomUUID();
        Instant now = clock.instant();

        events.insertAck(ackId, eventId, target, channel, role.name(), userId, OffsetDateTime.ofInstant(now, ZoneOffset.UTC));
        boolean sent = forwarder.forward(event.path("gateway_id").asString(null), new EventAckForwarder.Ack(
                eventId, target, ackId, channel, role.name(), String.valueOf(userId), now.toEpochMilli()));
        if (!sent) {
            log.info("Tier 0 로 바로 보내지 못해 저장만 함(다시 보냄): event_id={} ack_id={}", eventId, ackId);
        }
        // 다른 관제 화면도 확인을 알아 반복 소리를 멈추게 /topic/events 로 다시 보낸다(3.10). Tier 0 가 끊겨 있어도 보낸다
        publisher.publishEvent(new RiskEventsDelivered(List.of(eventId)));
        // 다시 읽지 않고 방금 만든 확인을 붙여 돌려준다
        ObjectNode added = ((tools.jackson.databind.node.ArrayNode) event.get("acks")).addObject();
        added.put("ack_id", ackId.toString());
        added.put("channel", channel);
        added.put("actor_role", role.name());
        added.put("actor_id", String.valueOf(userId));
        added.put("acked_at", now.toEpochMilli());
        return event;
    }
}
