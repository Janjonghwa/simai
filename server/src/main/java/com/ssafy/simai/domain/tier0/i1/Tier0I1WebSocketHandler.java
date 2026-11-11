package com.ssafy.simai.domain.tier0.i1;

import java.time.Clock;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.ObjectMapper;

/**
 * I1 Tier 0 → EC2 수신(명세 12.2·12.3, ai/services/tier0/i1_client.py 의 상대편). 인증은 업그레이드 때 끝났다(SecurityConfig).
 * 메시지 하나가 틀리거나 저장에 실패해도 연결은 끊지 않는다. 끊으면 Tier 0 가 재연결하며 같은 것을 다시 보내 같은 실패를 되풀이한다.
 * 그래서 메시지 하나를 처리하다 난 예외는 종류를 가리지 않고 여기서 받는다(DB 장애의 트랜잭션 예외, Jackson 타입 예외 포함).
 * 로그에는 msg_type·msg_id 만 남기고 payload 는 남기지 않는다(위치 기록, 11.4).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class Tier0I1WebSocketHandler extends TextWebSocketHandler {

    private static final Logger log = LoggerFactory.getLogger(Tier0I1WebSocketHandler.class);

    /**
     * world_state 한 개(사람 수십 명·골격 포함)는 컨테이너 기본 한도 8 KiB 를 넘는다. 넘으면 컨테이너가 연결을 닫는다(1009).
     * 컨테이너 전체가 아니라 I1 세션에만 올린다.
     */
    static final int MAX_TEXT_MESSAGE_BYTES = 1024 * 1024;

    private static final String CONNECTION = "simai.i1.connection";

    private final ObjectMapper mapper;
    private final I1MessageRouter router;
    private final I1ConnectionRegistry connections;
    private final Clock clock;

    public Tier0I1WebSocketHandler(ObjectMapper mapper, I1MessageRouter router, I1ConnectionRegistry connections, Clock clock) {
        this.mapper = mapper;
        this.router = router;
        this.connections = connections;
        this.clock = clock;
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) {
        session.setTextMessageSizeLimit(MAX_TEXT_MESSAGE_BYTES);
        I1Connection connection = new I1Connection(session, mapper, clock);
        session.getAttributes().put(CONNECTION, connection);
        connections.add(connection);
        log.info("I1 연결: session={} remote={}", session.getId(), session.getRemoteAddress());
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) {
        I1Connection connection = (I1Connection) session.getAttributes().get(CONNECTION);
        I1Envelope envelope;
        try {
            envelope = I1Envelope.parse(mapper.readTree(message.getPayload()));
            connection.bind(envelope.sourceId());
        } catch (RuntimeException e) {
            router.countInvalid(null);
            log.warn("I1 봉투를 읽지 못해 버림: session={} reason={}", session.getId(), reason(e));
            return;
        }
        // 받은 것만으로 Tier 0 가 살아 있다고 본다(저장 성공 여부와 상관없이, S15P31A101-595)
        connections.received(envelope.sourceId(), System.nanoTime());
        try {
            router.route(envelope, clock.instant(), connection);
        } catch (InvalidI1Message | JacksonException e) {
            router.countInvalid(envelope.msgType());
            log.warn("I1 계약 위반이라 버림: msg_type={} msg_id={} reason={}", envelope.msgType(), envelope.msgId(), reason(e));
            try {
                router.rejected(envelope, connection, reason(e));
            } catch (RuntimeException rejectFailure) {
                log.warn("I1 버린 메시지 처리 실패: msg_type={} error={}", envelope.msgType(),
                        rejectFailure.getClass().getSimpleName());
            }
        } catch (RuntimeException e) {
            router.countFailed(envelope.msgType());
            // DB 오류 문구에는 값이 실릴 수 있어 예외 종류만 남긴다
            log.warn("I1 저장 실패: msg_type={} msg_id={} error={}", envelope.msgType(), envelope.msgId(),
                    e.getClass().getSimpleName());
        }
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        connections.remove(session.getId());
        log.info("I1 연결 끊김: session={} status={}", session.getId(), status);
    }

    /** InvalidI1Message 문구는 칸 이름만 담는다. 그 밖의 예외 문구에는 값이 실릴 수 있어 종류만 남긴다. */
    private static String reason(RuntimeException e) {
        return e instanceof InvalidI1Message ? e.getMessage() : e.getClass().getSimpleName();
    }
}
