package com.ssafy.simai.domain.tier0.event;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyCollection;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doCallRealMethod;
import static org.mockito.Mockito.doThrow;

import com.ssafy.simai.domain.tier0.Tier0Paths;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.Predicate;
import java.util.function.Supplier;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.dao.TransientDataAccessResourceException;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 이벤트 전이 수신·재동기화(S15P31A101-106, 명세 3.2). 실제 포트 I1 로 Tier 0 처럼 전이·스냅샷을 보내고 DB 와 journal_ack 를 본다.
 * G3 D-G3-05: 동기화 중에 해소된 이벤트는 실시간 재알림 대상이 아니다(AUDIT_ONLY), 아직 활성이고 근거가 있으면 DELAYED_ACTIVE.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class RiskEventJournalIntegrationTest {

    private static final String TOKEN = "test-only-tier0-token";
    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    Tier0HeldEvents held;

    /** 스냅샷 분류에 일시적 DB 오류를 넣는다(-621). 시험마다 되돌린다 */
    @MockitoSpyBean
    RiskEventRepository events;

    private final String gatewayId = "tier0-journal-" + UUID.randomUUID();

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM risk_event_update WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM risk_event WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM journal_cursor WHERE gateway_id = ?", gatewayId);
    }

    @Test
    void 동기화_중_해소된_이벤트는_AUDIT_ONLY_아직_활성이면_DELAYED_ACTIVE_그_뒤는_REALTIME() throws Exception {
        UUID resolved = UUID.randomUUID();
        UUID stillActive = UUID.randomUUID();

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(resolved, 1, 0, "RAISED", true));
            tier0.send(transition(resolved, 2, 1, "RESOLVED", true));
            tier0.send(transition(stillActive, 1, 2, "RAISED", true));
            tier0.send(snapshot(2L, active(stillActive, 1, "RAISED", true)));

            JsonNode ack = tier0.awaitAck(a -> !a.get("sync_mode").booleanValue());
            assertThat(ack.get("acked_offset").longValue()).isEqualTo(2);

            assertThat(event(resolved, "lifecycle")).isEqualTo("RESOLVED");
            assertThat(event(resolved, "delivery")).isEqualTo("AUDIT_ONLY");
            assertThat(event(stillActive, "delivery")).isEqualTo("DELAYED_ACTIVE");
            assertThat(jdbc.queryForObject("SELECT count(*) FROM risk_event_update WHERE gateway_id = ? AND delivery_mode = 'AUDIT_ONLY'",
                    Integer.class, gatewayId)).isEqualTo(3);

            // 스냅샷 뒤에 온 전이는 실시간이다
            tier0.send(transition(stillActive, 2, 3, "UPDATED", false));
            await(() -> event(stillActive, "delivery"), "REALTIME");
            assertThat(jdbc.queryForObject("SELECT delivery_mode FROM risk_event_update WHERE event_id = ? AND event_sequence = 2",
                    String.class, stillActive)).isEqualTo("REALTIME");
            assertThat(jdbc.queryForObject("SELECT sync_mode FROM journal_cursor WHERE gateway_id = ?", Boolean.class, gatewayId))
                    .isFalse();
        }
    }

    @Test
    void 근거_없는_활성_이벤트는_알리지_않고_AUDIT_ONLY_로_둔다() throws Exception {
        UUID noEvidence = UUID.randomUUID();

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(noEvidence, 1, 0, "RAISED", true));
            tier0.send(snapshot(0L, active(noEvidence, 1, "RAISED", false)));
            tier0.awaitAck(a -> !a.get("sync_mode").booleanValue());
        }

        assertThat(event(noEvidence, "delivery")).isEqualTo("AUDIT_ONLY");
        assertThat(event(noEvidence, "lifecycle")).isEqualTo("RAISED");
    }

    @Test
    void 빈_구간은_journal_ack_로_다시_요청하고_채워지면_acked_가_나아간다() throws Exception {
        UUID event = UUID.randomUUID();

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 10, "RAISED", false));
            tier0.send(transition(event, 3, 12, "UPDATED", false));

            JsonNode resend = tier0.awaitAck(a -> a.has("resend_from_offset"));
            assertThat(resend.get("acked_offset").longValue()).isEqualTo(10);
            assertThat(resend.get("resend_from_offset").longValue()).isEqualTo(11);
            assertThat(resend.get("sync_mode").booleanValue()).isTrue();

            tier0.send(transition(event, 2, 11, "UPDATED", true));
            JsonNode filled = tier0.awaitAck(a -> a.get("acked_offset").longValue() == 12);
            assertThat(filled.has("resend_from_offset")).isFalse();
        }

        assertThat(jdbc.queryForObject("SELECT acked_offset FROM journal_cursor WHERE gateway_id = ?", Long.class, gatewayId))
                .isEqualTo(12);
        assertThat(jdbc.queryForObject("SELECT event_sequence FROM risk_event WHERE event_id = ?", Integer.class, event))
                .isEqualTo(3);
    }

    @Test
    void 늦게_온_낮은_sequence_는_기록만_하고_같은_전이는_다시_넣지_않는다() throws Exception {
        UUID event = UUID.randomUUID();
        ObjectNode raised = transition(event, 1, 21, "RAISED", false);

        try (Tier0 tier0 = connect()) {
            // RAISED 보다 UPDATED 가 먼저 와도 부모 이벤트를 만들고 기록한다
            tier0.send(transition(event, 2, 20, "UPDATED", false));
            tier0.send(raised);
            tier0.send(raised);
            tier0.awaitAck(a -> a.get("acked_offset").longValue() == 21);
        }

        assertThat(event(event, "lifecycle")).isEqualTo("UPDATED");
        assertThat(jdbc.queryForObject("SELECT event_sequence FROM risk_event WHERE event_id = ?", Integer.class, event)).isEqualTo(2);
        // RAISED 가 늦게 와도 처음 시각은 채운다
        assertThat(event(event, "raised_at")).isNotNull();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM risk_event_update WHERE event_id = ?", Integer.class, event)).isEqualTo(2);
    }

    @Test
    void 계약_위반_전이는_건너뛰어_빈_구간이_남지_않고_동기화가_끝난다() throws Exception {
        UUID event = UUID.randomUUID();
        ObjectNode broken = transition(event, 2, 1, "UPDATED", true);
        ((ObjectNode) broken.get("payload")).put("event_sequence", 0);

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", true));
            tier0.send(broken);
            tier0.send(transition(event, 3, 2, "UPDATED", true));
            tier0.send(snapshot(2L, active(event, 3, "UPDATED", true)));

            JsonNode ack = tier0.awaitAck(a -> !a.get("sync_mode").booleanValue());
            assertThat(ack.get("acked_offset").longValue()).isEqualTo(2);
        }

        assertThat(event(event, "delivery")).isEqualTo("DELAYED_ACTIVE");
    }

    @Test
    void Tier0_저널이_새로_시작되면_세대를_올려_같은_offset_의_새_전이를_기록한다() throws Exception {
        UUID before = UUID.randomUUID();
        UUID after = UUID.randomUUID();

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(before, 1, 0, "RAISED", false));
            tier0.send(transition(before, 2, 1, "UPDATED", false));
            tier0.awaitAck(a -> a.get("acked_offset").longValue() == 1);
        }
        // Pod 가 바뀌어 저널이 비었다: offset 0 부터 다른 전이
        try (Tier0 tier0 = connect()) {
            tier0.send(transition(after, 1, 0, "RAISED", false));
            await(() -> String.valueOf(jdbc.queryForObject(
                    "SELECT count(*) FROM risk_event WHERE event_id = ?", Integer.class, after)), "1");
        }

        assertThat(jdbc.queryForObject("SELECT journal_epoch FROM journal_cursor WHERE gateway_id = ?", Integer.class, gatewayId))
                .isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT journal_epoch FROM risk_event_update WHERE event_id = ?", Integer.class, after))
                .isEqualTo(1);
    }

    @Test
    void 끝나지_못한_동기화에서_만든_이벤트도_다음_스냅샷에서_분류한다() throws Exception {
        UUID event = UUID.randomUUID();

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", true));
            tier0.awaitAck(a -> a.get("acked_offset").longValue() == 0);
        }
        // 다음 연결: 이미 ack 했으니 다시 보내지 않고 스냅샷만 온다
        try (Tier0 tier0 = connect()) {
            tier0.send(snapshot(0L, active(event, 1, "RAISED", true)));
            tier0.awaitAck(a -> !a.get("sync_mode").booleanValue());
        }

        assertThat(event(event, "delivery")).isEqualTo("DELAYED_ACTIVE");
    }

    @Test
    void 스냅샷_분류가_실패하면_연결을_닫고_다시_붙은_뒤_새_스냅샷으로_분류한다() throws Exception {
        // 같은 연결에서는 Tier 0 가 스냅샷을 다시 보내지 않는다. 열어 두면 끊길 때까지 모든 전이가 AUDIT_ONLY 로 남았다(감사 NC1, -621)
        UUID raised = UUID.randomUUID();
        doThrow(new TransientDataAccessResourceException("시험: 일시적 DB 오류")).doCallRealMethod()
                .when(events).setDelivery(anyCollection(), eq("DELAYED_ACTIVE"));

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(raised, 1, 0, "RAISED", true));
            tier0.awaitAck(a -> a.get("acked_offset").longValue() == 0);
            tier0.send(snapshot(0L, active(raised, 1, "RAISED", true)));
            tier0.awaitClosed();
        }
        // 분류가 되돌려져 전달 방식은 아직 정하지 않았다(분류 대기 NULL). 다음 스냅샷이 정한다
        assertThat(event(raised, "delivery")).isNull();
        assertThat(jdbc.queryForObject("SELECT sync_mode FROM journal_cursor WHERE gateway_id = ?", Boolean.class, gatewayId))
                .isTrue();

        // 다시 붙은 Tier 0 가 새 스냅샷을 보내면 분류가 끝나고 지연 알림 대상이 된다
        try (Tier0 tier0 = connect()) {
            tier0.send(snapshot(0L, active(raised, 1, "RAISED", true)));
            tier0.awaitAck(a -> !a.get("sync_mode").booleanValue());
        }
        assertThat(event(raised, "delivery")).isEqualTo("DELAYED_ACTIVE");
    }

    @Test
    void 동기화_중_새로_난_이벤트도_Tier0_가_들고_있는_것으로_본다() throws Exception {
        // 스냅샷 전 동기화 중 RAISED 는 알리지 않지만(AUDIT_ONLY) 「지난 관제」로 내려가지 않게 들고 있음에 넣는다(-595, -621)
        UUID raised = UUID.randomUUID();
        UUID resolved = UUID.randomUUID();

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(raised, 1, 0, "RAISED", false));
            tier0.send(transition(resolved, 1, 1, "RAISED", false));
            tier0.send(transition(resolved, 2, 2, "RESOLVED", false));
            tier0.awaitAck(a -> a.get("acked_offset").longValue() == 2 && a.get("sync_mode").booleanValue());

            // 동기화 중 전이는 알리지 않는다(전이 기록은 AUDIT_ONLY, 이벤트 전달 방식은 스냅샷 분류 대기)
            assertThat(event(raised, "delivery")).isNull();
            assertThat(jdbc.queryForObject("SELECT delivery_mode FROM risk_event_update WHERE event_id = ?", String.class, raised))
                    .isEqualTo("AUDIT_ONLY");
            assertThat(held.isHeld(gatewayId, raised)).isTrue();
            assertThat(held.isHeld(gatewayId, resolved)).isFalse();
        }
    }

    // ---- 도우미

    private ObjectNode transition(UUID eventId, int sequence, long offset, String kind, boolean resent) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("risk_event_transition.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("gateway_id", gatewayId);
        p.put("event_id", eventId.toString());
        p.put("event_sequence", sequence);
        p.put("journal_offset", offset);
        p.put("transition", kind);
        p.put("resent", resent);
        return root;
    }

    private ObjectNode snapshot(Long offset, ObjectNode... activeEvents) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("event_state_snapshot.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("gateway_id", gatewayId);
        p.put("journal_offset", offset);
        ArrayNode active = p.putArray("active_events");
        for (ObjectNode event : activeEvents) {
            active.add(event);
        }
        return root;
    }

    private ObjectNode active(UUID eventId, int sequence, String lifecycle, boolean hasEvidence) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("event_state_snapshot.json")));
        ObjectNode event = (ObjectNode) root.path("payload").path("active_events").get(0);
        event.put("event_id", eventId.toString());
        event.put("event_sequence", sequence);
        event.put("lifecycle", lifecycle);
        if (hasEvidence) {
            event.put("last_valid_observation_at", 1760342459900L);
        } else {
            event.putNull("last_valid_observation_at");
        }
        return event;
    }

    private String event(UUID eventId, String column) {
        return jdbc.queryForObject("SELECT " + column + " FROM risk_event WHERE event_id = ?", String.class, eventId);
    }

    private static void await(Supplier<String> actual, String expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (!expected.equals(actual.get()) && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(actual.get()).isEqualTo(expected);
    }

    private Tier0 connect() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TOKEN);
        BlockingQueue<JsonNode> received = new LinkedBlockingQueue<>();
        WebSocketSession session = new StandardWebSocketClient().execute(new TextWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                received.add(MAPPER.readTree(message.getPayload()));
            }
        }, headers, URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS);
        return new Tier0(session, received);
    }

    /** Tier 0 흉내. EC2 가 보낸 메시지를 모은다 */
    private record Tier0(WebSocketSession session, BlockingQueue<JsonNode> received) implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(MAPPER.writeValueAsString(message)));
        }

        /** 조건에 맞는 journal_ack 를 기다린다(1초 주기로 몰아 보내므로 넉넉히) */
        JsonNode awaitAck(Predicate<JsonNode> matches) throws InterruptedException {
            long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
            while (System.nanoTime() < deadline) {
                JsonNode message = received.poll(200, TimeUnit.MILLISECONDS);
                if (message != null && "journal_ack".equals(message.path("msg_type").asString())
                        && "ec2".equals(message.path("source_id").asString())
                        && matches.test(message.get("payload"))) {
                    return message.get("payload");
                }
            }
            throw new AssertionError("기다린 journal_ack 가 오지 않았다");
        }

        /** EC2 가 이 연결을 닫을 때까지 기다린다 */
        void awaitClosed() throws InterruptedException {
            long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
            while (session.isOpen() && System.nanoTime() < deadline) {
                Thread.sleep(50);
            }
            assertThat(session.isOpen()).as("EC2 가 연결을 닫았다").isFalse();
        }

        @Override
        public void close() throws Exception {
            if (session.isOpen()) {
                session.close();
            }
        }
    }
}
