package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.net.URI;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * S15P31A101-510 워치 배정 알림(watch_binding_notice) 통합 시험. 시험용 워치 WebSocket 클라이언트로 받는다.
 * <ul>
 * <li>연결하면 지금 상태를 한 번(배정 없음 UNBOUND), 배정이 있으면 BOUND 로 받는다</li>
 * <li>수동·움직임 대조 배정이 생기면 BOUND 한 번, 풀리면(RELEASED·MATCH_LOST) UNBOUND 한 번. 모두 계약 검사를 지난다</li>
 * <li>같은 상태는 반복하지 않는다(같은 World ID 를 다시 이어도 BOUND 를 또 보내지 않는다)</li>
 * <li>다른 워치로 옮기면 옛 워치는 UNBOUND(REPLACED), 새 워치는 BOUND</li>
 * <li>끊긴 동안의 변화는 보내지 않고, 다시 연결하면 지금 상태를 한 번 보낸다</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class WatchBindingNoticeIntegrationTest {

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    MachineTokenService machineTokens;

    @Autowired
    WatchBindings bindings;

    @Autowired
    WatchBindingNotifier notifier;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    ObjectMapper mapper;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final List<AutoCloseable> closeables = new ArrayList<>();
    private String watchId;
    private String watchToken;
    private String otherId;
    private String otherToken;
    private String worldId;
    private long adminId;

    @BeforeEach
    void setUp() {
        watchId = "watch-notice-" + tag;
        watchToken = machineTokens.registerDevice(watchId, "WATCH").token();
        otherId = "watch-notice-o-" + tag;
        otherToken = machineTokens.registerDevice(otherId, "WATCH").token();
        worldId = "w-" + tag;
        Long id = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "watch-notice-admin-" + tag);
        adminId = id == null ? -1 : id;
    }

    @AfterEach
    void tearDown() throws Exception {
        for (AutoCloseable c : closeables) {
            try {
                c.close();
            } catch (Exception e) {
                // 이미 닫힘
            }
        }
        waitUntilClosed();
        jdbc.update("DELETE FROM watch_binding WHERE device_id IN (?, ?)", watchId, otherId);
        jdbc.update("DELETE FROM watch_connection WHERE device_id IN (?, ?)", watchId, otherId);
        jdbc.update("DELETE FROM device WHERE device_id IN (?, ?)", watchId, otherId);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 연결하면_배정_없음을_한_번_받고_배정이_생기면_BOUND_풀리면_UNBOUND_를_한_번씩_받는다() throws Exception {
        Watch watch = watch(watchId, watchToken);
        JsonNode first = watch.notice();
        assertValid(first);
        assertThat(first.path("source_id").asString()).isEqualTo("ec2");
        assertThat(first.path("payload").path("state").asString()).isEqualTo("UNBOUND");
        assertThat(first.path("payload").path("world_id").isNull()).isTrue();
        assertThat(first.path("payload").has("unbind_reason")).as("초기 동기화는 사유를 모른다").isFalse();

        bindings.bindManually(watchId, worldId, adminId);
        JsonNode bound = watch.notice();
        assertValid(bound);
        assertThat(bound.path("payload").path("state").asString()).isEqualTo("BOUND");
        assertThat(bound.path("payload").path("method").asString()).isEqualTo("MANUAL");
        assertThat(bound.path("payload").path("world_id").asString()).isEqualTo(worldId);
        assertThat(watch.noNotice()).as("배정은 한 번만 알린다").isTrue();

        bindings.release(watchId);
        JsonNode released = watch.notice();
        assertValid(released);
        assertThat(released.path("payload").path("state").asString()).isEqualTo("UNBOUND");
        assertThat(released.path("payload").path("unbind_reason").asString()).isEqualTo("RELEASED");
        assertThat(watch.noNotice()).isTrue();
    }

    @Test
    void 움직임_대조_배정과_대조_해제도_알린다() throws Exception {
        Watch watch = watch(watchId, watchToken);
        watch.notice(); // 연결 직후 UNBOUND

        assertThat(bindings.bindByMotion(watchId, worldId, 0.9)).isTrue();
        JsonNode bound = watch.notice();
        assertValid(bound);
        assertThat(bound.path("payload").path("method").asString()).isEqualTo("MOTION_MATCH");
        assertThat(bound.path("payload").path("world_id").asString()).isEqualTo(worldId);

        // 대조가 같은 World ID 를 다시 이어도(옛 행 REPLACED + 새 행) 같은 상태라 다시 보내지 않는다
        assertThat(bindings.bindByMotion(watchId, worldId, 0.8)).isTrue();
        assertThat(watch.noNotice()).as("같은 상태 반복 없음").isTrue();

        assertThat(bindings.unbindMotion(watchId, worldId, "MATCH_LOST")).isEqualTo(1);
        JsonNode lost = watch.notice();
        assertValid(lost);
        assertThat(lost.path("payload").path("state").asString()).isEqualTo("UNBOUND");
        assertThat(lost.path("payload").path("unbind_reason").asString()).isEqualTo("MATCH_LOST");
    }

    @Test
    void 같은_World_ID_를_수동으로_다시_이어도_BOUND_를_반복하지_않는다() throws Exception {
        Watch watch = watch(watchId, watchToken);
        watch.notice();
        bindings.bindManually(watchId, worldId, adminId);
        assertThat(watch.notice().path("payload").path("state").asString()).isEqualTo("BOUND");
        bindings.bindManually(watchId, worldId, adminId);
        assertThat(watch.noNotice()).isTrue();
        // 다른 World ID 로 바뀌면 새 상태다
        bindings.bindManually(watchId, worldId + "-2", adminId);
        JsonNode changed = watch.notice();
        assertThat(changed.path("payload").path("world_id").asString()).isEqualTo(worldId + "-2");
    }

    @Test
    void 다른_워치로_옮기면_옛_워치는_UNBOUND_새_워치는_BOUND_를_받는다() throws Exception {
        Watch first = watch(watchId, watchToken);
        Watch other = watch(otherId, otherToken);
        first.notice();
        other.notice();
        bindings.bindManually(watchId, worldId, adminId);
        assertThat(first.notice().path("payload").path("state").asString()).isEqualTo("BOUND");

        bindings.bindManually(otherId, worldId, adminId);
        JsonNode oldWatch = first.notice();
        assertValid(oldWatch);
        assertThat(oldWatch.path("payload").path("state").asString()).isEqualTo("UNBOUND");
        assertThat(oldWatch.path("payload").path("unbind_reason").asString()).isEqualTo("REPLACED");
        JsonNode newWatch = other.notice();
        assertThat(newWatch.path("payload").path("state").asString()).isEqualTo("BOUND");
        assertThat(newWatch.path("payload").path("world_id").asString()).isEqualTo(worldId);
    }

    @Test
    void 끊긴_워치에는_보내지_않고_다시_연결하면_지금_상태를_한_번_보낸다() throws Exception {
        Watch watch = watch(watchId, watchToken);
        watch.notice();
        watch.close();
        waitUntilClosed();

        bindings.bindManually(watchId, worldId, adminId); // 끊긴 동안
        notifier.awaitIdle();

        Watch again = watch(watchId, watchToken);
        JsonNode sync = again.notice();
        assertValid(sync);
        assertThat(sync.path("payload").path("state").asString()).isEqualTo("BOUND");
        assertThat(sync.path("payload").path("world_id").asString()).isEqualTo(worldId);
        assertThat(again.noNotice()).as("동기화는 한 번").isTrue();

        // 이미 BOUND 였어도 다시 연결하면 한 번 더 보낸다(워치 쪽 상태는 사라졌을 수 있다)
        again.close();
        waitUntilClosed();
        Watch third = watch(watchId, watchToken);
        assertThat(third.notice().path("payload").path("state").asString()).isEqualTo("BOUND");
    }

    // ------------------------------------------------------------------ 도우미

    private void assertValid(JsonNode message) {
        assertThat(schemas.violations("messages/watch_binding_notice", message.toString())).isEmpty();
    }

    private void waitUntilClosed() throws InterruptedException {
        long deadline = System.nanoTime() + 3_000_000_000L;
        while (System.nanoTime() < deadline) {
            Integer open = jdbc.queryForObject("SELECT count(*) FROM watch_connection WHERE device_id IN (?, ?) AND ended_at IS NULL",
                    Integer.class, watchId, otherId);
            if (open == null || open == 0) {
                return;
            }
            Thread.sleep(50);
        }
    }

    private ObjectNode heartbeat(String sourceId) {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "watch_heartbeat");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", System.currentTimeMillis());
        root.put("source_id", sourceId);
        ObjectNode p = root.putObject("payload");
        p.put("heartbeat_seq", 0);
        ObjectNode w = p.putObject("watch");
        w.put("battery_pct", 80);
        w.put("transport", "WIFI");
        return root;
    }

    /** 붙고 첫 하트비트 답을 받은 워치. 시험 동안 0.5초마다 하트비트를 보내 끊김에 걸리지 않는다 */
    private Watch watch(String deviceId, String token) throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        Watch watch = new Watch(mapper);
        watch.session = new StandardWebSocketClient().execute(watch.handler, headers,
                URI.create("ws://localhost:" + port + WatchPaths.WS)).get(5, TimeUnit.SECONDS);
        closeables.add(watch);
        watch.send(heartbeat(deviceId));
        Thread.ofVirtual().start(() -> {
            while (watch.session.isOpen()) {
                try {
                    Thread.sleep(500);
                    watch.send(heartbeat(deviceId));
                } catch (Exception e) {
                    return;
                }
            }
        });
        return watch;
    }

    /** 시험용 워치. 배정 알림만 모은다(하트비트 답은 버린다) */
    private final class Watch implements AutoCloseable {
        final BlockingQueue<JsonNode> notices = new LinkedBlockingQueue<>();
        final ObjectMapper mapper;
        WebSocketSession session;
        final TextWebSocketHandler handler = new TextWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                JsonNode m = Watch.this.mapper.readTree(message.getPayload());
                if ("watch_binding_notice".equals(m.path("msg_type").asString())) {
                    notices.add(m);
                }
            }
        };

        Watch(ObjectMapper mapper) {
            this.mapper = mapper;
        }

        /** 다음 배정 알림 */
        JsonNode notice() throws Exception {
            notifier.awaitIdle();
            JsonNode m = notices.poll(5, TimeUnit.SECONDS);
            assertThat(m).as("배정 알림이 5초 안에 와야 한다").isNotNull();
            return m;
        }

        /** 잠깐 기다려도 더 오지 않는다 */
        boolean noNotice() throws Exception {
            notifier.awaitIdle();
            return notices.poll(500, TimeUnit.MILLISECONDS) == null;
        }

        synchronized void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(mapper.writeValueAsString(message)));
        }

        @Override
        public void close() throws Exception {
            if (session != null && session.isOpen()) {
                session.close();
            }
        }
    }
}
