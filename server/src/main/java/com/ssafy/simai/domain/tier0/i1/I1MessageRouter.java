package com.ssafy.simai.domain.tier0.i1;

import io.micrometer.core.instrument.MeterRegistry;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;

/**
 * I1 위로(Tier 0 → EC2) 메시지를 msg_type 으로 저장기에 넘긴다(명세 12.3).
 * 결과는 simai.i1.messages{msg_type, result} 로 센다.
 * result: stored · duplicate(재전송이라 바뀐 것 없음) · invalid(계약 위반) · failed(저장 실패) · unsupported_version · unknown_type.
 * msg_type 태그는 저장기가 있는 종류만 쓰고 나머지는 other 다. 보낸 쪽 문자열로 지표 수가 늘지 않게 한다.
 * 아직 저장기가 없는 종류(risk_event_transition 등)는 unknown_type 으로 세고 버린다. 붙이는 이슈가 저장기를 더한다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class I1MessageRouter {

    private static final Logger log = LoggerFactory.getLogger(I1MessageRouter.class);
    static final String METRIC = "simai.i1.messages";
    static final String OTHER = "other";

    private final Map<String, I1MessageIngestor> ingestors;
    private final MeterRegistry meters;

    public I1MessageRouter(List<I1MessageIngestor> ingestors, MeterRegistry meters) {
        this.ingestors = ingestors.stream().collect(Collectors.toUnmodifiableMap(I1MessageIngestor::msgType, Function.identity()));
        this.meters = meters;
    }

    /** 계약 위반은 InvalidI1Message 로, 저장 실패는 그 예외로 던진다. 세는 일은 호출자가 countInvalid·countFailed 로 한다. */
    public void route(I1Envelope envelope, Instant receivedAt) {
        I1MessageIngestor ingestor = ingestors.get(envelope.msgType());
        if (ingestor == null) {
            count(envelope.msgType(), "unknown_type");
            log.debug("I1 저장기 없음: msg_type={} msg_id={}", envelope.msgType(), envelope.msgId());
            return;
        }
        if (!ingestor.supportedMajors().contains(envelope.major())) {
            count(envelope.msgType(), "unsupported_version");
            log.warn("I1 메이저 버전을 읽을 수 없어 버림: msg_type={} schema_version={} msg_id={}",
                    envelope.msgType(), envelope.schemaVersion(), envelope.msgId());
            return;
        }
        I1MessageIngestor.Outcome outcome = ingestor.ingest(envelope, receivedAt);
        count(envelope.msgType(), outcome == I1MessageIngestor.Outcome.STORED ? "stored" : "duplicate");
    }

    /** 봉투나 payload 가 계약과 맞지 않는 메시지. 봉투를 못 읽었으면 msgType 은 null 이다. */
    void countInvalid(String msgType) {
        count(msgType, "invalid");
    }

    void countFailed(String msgType) {
        count(msgType, "failed");
    }

    private void count(String msgType, String result) {
        String tag = msgType != null && ingestors.containsKey(msgType) ? msgType : OTHER;
        meters.counter(METRIC, "msg_type", tag, "result", result).increment();
    }
}
