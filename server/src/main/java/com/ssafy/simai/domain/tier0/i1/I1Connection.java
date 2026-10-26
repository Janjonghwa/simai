package com.ssafy.simai.domain.tier0.i1;

import java.io.IOException;
import java.time.Clock;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Supplier;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.ConcurrentWebSocketSessionDecorator;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * I1 연결 하나(Tier 0 하나). 첫 메시지의 source_id 로 gateway_id 가 정해지고, 그 뒤 다른 source_id 는 받지 않는다.
 * 저장기가 연결별 상태(저널 수신 위치 등)를 두고, EC2 → Tier 0 메시지(journal_ack 등)를 보낸다.
 * 보내기는 수신 스레드와 주기 작업 스레드가 함께 하므로 ConcurrentWebSocketSessionDecorator 로 감싼다.
 */
public final class I1Connection {

    /** EC2 가 보내는 메시지의 source_id(명세 12.1) */
    public static final String EC2_SOURCE_ID = "ec2";

    private static final int SEND_TIME_LIMIT_MS = 5_000;
    private static final int SEND_BUFFER_LIMIT_BYTES = 512 * 1024;

    private final WebSocketSession session;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final Map<String, Object> state = new ConcurrentHashMap<>();
    private volatile String gatewayId;

    I1Connection(WebSocketSession session, ObjectMapper mapper, Clock clock) {
        this.session = new ConcurrentWebSocketSessionDecorator(session, SEND_TIME_LIMIT_MS, SEND_BUFFER_LIMIT_BYTES);
        this.mapper = mapper;
        this.clock = clock;
    }

    public String id() {
        return session.getId();
    }

    /** 첫 메시지 전에는 null */
    public String gatewayId() {
        return gatewayId;
    }

    public boolean isOpen() {
        return session.isOpen();
    }

    synchronized void bind(String sourceId) {
        if (gatewayId == null) {
            gatewayId = sourceId;
        } else if (!gatewayId.equals(sourceId)) {
            throw new InvalidI1Message("source_id 가 이 연결의 Tier 0 와 다르다");
        }
    }

    /** 연결별 상태. 연결이 끊기면 함께 사라진다 */
    @SuppressWarnings("unchecked")
    public <T> T state(String key, Supplier<T> initial) {
        return (T) state.computeIfAbsent(key, k -> initial.get());
    }

    /** 아직 만들지 않았으면 null(주기 작업이 상태를 새로 만들지 않게) */
    @SuppressWarnings("unchecked")
    public <T> T stateIfPresent(String key) {
        return (T) state.get(key);
    }

    /** 연결을 닫는다. Tier 0 는 다시 붙어 자기 acked 다음부터 보낸다 */
    public void close(String reason) {
        try {
            session.close(org.springframework.web.socket.CloseStatus.SERVICE_RESTARTED.withReason(reason));
        } catch (IOException e) {
            // 이미 닫혔다
        }
    }

    /** 공통 봉투를 씌워 보낸다(명세 12.1). */
    public void send(String msgType, String schemaVersion, ObjectNode payload) throws IOException {
        ObjectNode envelope = mapper.createObjectNode();
        envelope.put("schema_version", schemaVersion);
        envelope.put("msg_type", msgType);
        envelope.put("msg_id", UUID.randomUUID().toString());
        envelope.put("sent_at", clock.millis());
        envelope.put("source_id", EC2_SOURCE_ID);
        envelope.set("payload", payload);
        session.sendMessage(new TextMessage(mapper.writeValueAsString(envelope)));
    }

    public ObjectNode newPayload() {
        return mapper.createObjectNode();
    }
}
