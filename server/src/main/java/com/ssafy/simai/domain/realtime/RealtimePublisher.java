package com.ssafy.simai.domain.realtime;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Clock;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.messaging.MessageHeaders;
import org.springframework.messaging.simp.SimpMessageHeaderAccessor;
import org.springframework.messaging.simp.SimpMessagingTemplate;
import org.springframework.messaging.support.MessageBuilder;
import org.springframework.stereotype.Component;
import org.springframework.util.MimeTypeUtils;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 공통 봉투(명세 12.1, source_id ec2)를 씌워 STOMP 토픽에 JSON 으로 보낸다.
 * 결과는 simai.i2.messages{topic, result} 로 센다. result: sent · replaced(최신 것만 보내 버림) · failed.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class RealtimePublisher {

    static final String METRIC = "simai.i2.messages";
    private static final Logger log = LoggerFactory.getLogger(RealtimePublisher.class);

    private final SimpMessagingTemplate template;
    private final ObjectMapper mapper;
    private final MeterRegistry meters;
    private final Clock clock;

    RealtimePublisher(SimpMessagingTemplate template, ObjectMapper mapper, MeterRegistry meters, Clock clock) {
        this.template = template;
        this.mapper = mapper;
        this.meters = meters;
        this.clock = clock;
    }

    /** 보내지 못하면 세고 로그만 남긴다. 실시간 전달 실패가 저장·I1 수신을 멈추지 않게 한다 */
    public void publish(String topic, String msgType, String schemaVersion, JsonNode payload) {
        ObjectNode envelope = I1Envelope.outbound(mapper, clock.millis(), msgType, schemaVersion, payload);
        try {
            SimpMessageHeaderAccessor headers = SimpMessageHeaderAccessor.create();
            headers.setHeader(MessageHeaders.CONTENT_TYPE, MimeTypeUtils.APPLICATION_JSON);
            headers.setLeaveMutable(true);
            template.send(topic, MessageBuilder.createMessage(mapper.writeValueAsBytes(envelope), headers.getMessageHeaders()));
            count(topic, "sent");
        } catch (RuntimeException e) {
            count(topic, "failed");
            log.warn("STOMP 발행 실패: topic={} msg_type={}", topic, msgType, e);
        }
    }

    /** 보내지 않은 결과(replaced·failed)를 센다. 다른 패키지의 중계기(PPE 등)도 같은 지표를 쓴다 */
    public void count(String topic, String result) {
        meters.counter(METRIC, "topic", topic, "result", result).increment();
    }
}
