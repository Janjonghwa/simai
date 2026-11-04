package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.health.ViewerHealthService;
import com.ssafy.simai.domain.machine.MachineKind;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.net.URI;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * S15P31A101-336 워치 I4 연결 통합 시험(시험용 WebSocket 클라이언트 → /ws/watch). 시험 설정은 하트비트 주기 1초(끊김 3초), 판정 간격 0.2초.
 * <ul>
 * <li>업그레이드: 토큰 없음·틀림·폐기된 워치·노트북 토큰은 거부, 등록된 워치 토큰만 받는다</li>
 * <li>watch_heartbeat 에 echo(받은 seq·EC2 수신 시각)·interval_ms 로 답하고, 답은 계약을 지킨다</li>
 * <li>viewer_health.watches: 하트비트 중 CONNECTED(배터리·전송 경로), 하트비트가 끊기면 기한 안에 LOST 로 바뀌고 연결을 닫는다</li>
 * <li>연결 중 토큰 폐기는 다음 하트비트에서 닫고, 같은 워치의 새 연결은 옛 연결을 닫는다. 연결 기록 watch_connection 의 끝 사유</li>
 * <li>source_id 가 다르거나 계약을 어긴 하트비트는 버린다(답 없음, 연결 유지)</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class WatchLinkIntegrationTest {

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    MachineTokenService machineTokens;

    @Autowired
    ViewerHealthService viewerHealth;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    WatchRepository repository;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final List<Client> clients = new ArrayList<>();
    private String watchId;
    private String watchToken;
    private String laptopId;
    private String laptopToken;

    @BeforeEach
    void setUp() {
        watchId = "watch-link-" + tag;
        watchToken = machineTokens.registerDevice(watchId, "WATCH").token();
        laptopId = "laptop-link-" + tag;
        laptopToken = machineTokens.registerDevice(laptopId, "LAPTOP").token();
    }

    @AfterEach
    void tearDown() throws Exception {
        for (Client c : clients) {
            c.close();
        }
        // 서버가 닫힘을 기록할 때까지 기다린 뒤 지운다(FK)
        waitUntil(() -> openConnections() == 0, 3_000);
        jdbc.update("DELETE FROM watch_connection WHERE device_id = ?", watchId);
        jdbc.update("DELETE FROM device WHERE device_id IN (?, ?)", watchId, laptopId);
    }

    // ------------------------------------------------------------------ 인증

    @Test
    void 업그레이드는_등록된_워치_토큰만_받는다() {
        assertThatThrownBy(() -> connect(null)).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> connect("wrong-token")).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> connect(laptopToken)).as("노트북 토큰으로 워치 행세를 못 한다").isInstanceOf(ExecutionException.class);

        machineTokens.revoke(MachineKind.DEVICE, watchId);
        assertThatThrownBy(() -> connect(watchToken)).as("폐기된 워치").isInstanceOf(ExecutionException.class);
        assertThat(rows()).isEmpty();
    }

    // ------------------------------------------------------------------ 하트비트

    @Test
    void 하트비트에_echo와_interval로_답하고_연결_상태를_싣는다() throws Exception {
        Client watch = connect(watchToken);
        long before = System.currentTimeMillis();
        watch.send(heartbeat(watchId, 0, 72, "WIFI"));

        JsonNode reply = watch.next();
        assertThat(schemas.violations("messages/watch_heartbeat", reply.toString())).isEmpty();
        assertThat(reply.path("source_id").asString()).isEqualTo("ec2");
        assertThat(reply.path("payload").path("heartbeat_seq").asLong()).isZero();
        assertThat(reply.path("payload").path("interval_ms").asLong()).isEqualTo(1_000);
        assertThat(reply.path("payload").path("echo").path("seq").asLong()).isZero();
        assertThat(reply.path("payload").path("echo").path("received_at").asLong()).isBetween(before, System.currentTimeMillis());

        watch.send(heartbeat(watchId, 1, 71, "PHONE_BLUETOOTH"));
        JsonNode second = watch.next();
        assertThat(second.path("payload").path("heartbeat_seq").asLong()).as("EC2 seq 는 연결마다 0 부터 늘어난다").isEqualTo(1);
        assertThat(second.path("payload").path("echo").path("seq").asLong()).isEqualTo(1);

        JsonNode item = watchItem();
        assertThat(item.path("link").asString()).isEqualTo("CONNECTED");
        assertThat(item.path("battery_pct").asInt()).isEqualTo(71);
        assertThat(item.path("transport").asString()).isEqualTo("PHONE_BLUETOOTH");
        assertThat(item.path("last_heartbeat_at").asLong()).isGreaterThanOrEqualTo(before);

        Map<String, Object> row = rows().getFirst();
        assertThat(row.get("first_heartbeat_at")).isNotNull();
        assertThat(row.get("transport")).as("전송 경로가 바뀌면 기록을 고친다").isEqualTo("PHONE_BLUETOOTH");
        assertThat(row.get("app_version")).isEqualTo("0.1.0");
        assertThat(row.get("ended_at")).isNull();
        assertThat(jdbc.queryForObject("SELECT last_heartbeat_at IS NOT NULL FROM device WHERE device_id = ?", Boolean.class, watchId))
                .isTrue();
    }

    @Test
    void 하트비트가_끊기면_기한_안에_LOST로_바뀌고_연결을_닫는다() throws Exception {
        Client watch = connect(watchToken);
        // 끊김 기준(3초)보다 오래 하트비트를 이어 가면 끊기지 않는다
        for (int seq = 0; seq < 8; seq++) {
            watch.send(heartbeat(watchId, seq, 80, "WIFI"));
            watch.next();
            Thread.sleep(500);
        }
        assertThat(watchItem().path("link").asString()).isEqualTo("CONNECTED");
        assertThat(watch.closed).isEmpty();

        long stopped = System.nanoTime();
        CloseStatus status = watch.closed.poll(6, TimeUnit.SECONDS);
        long elapsedMs = (System.nanoTime() - stopped) / 1_000_000;
        assertThat(status).as("하트비트 3회 누락 + 판정 간격 안에 닫는다").isNotNull();
        assertThat(status.getCode()).isEqualTo(WatchHub.HEARTBEAT_TIMEOUT.getCode());
        // 마지막 하트비트는 0.5초 전에 보냈다: 3초 − 0.5초 ~ 3초 + 판정 간격(0.2초) + 여유
        assertThat(elapsedMs).isBetween(2_000L, 4_500L);

        JsonNode item = watchItem();
        assertThat(item.path("link").asString()).isEqualTo("LOST");
        assertThat(item.path("battery_pct").isNull()).isTrue();
        assertThat(item.path("transport").isNull()).isTrue();
        assertThat(item.path("last_heartbeat_at").isNull()).as("끊겨도 마지막 수신 시각은 남는다").isFalse();
        waitUntil(() -> openConnections() == 0, 2_000);
        assertThat(rows().getFirst().get("end_reason")).isEqualTo("HEARTBEAT_TIMEOUT");
    }

    @Test
    void 하트비트_없이_붙기만_한_연결도_기한이_지나면_닫는다() throws Exception {
        Client watch = connect(watchToken);
        // 클라이언트 업그레이드가 끝난 뒤 서버가 연결을 등록한다
        waitUntil(() -> "CONNECTED".equals(watchItem().path("link").asString()), 2_000);
        assertThat(watchItem().path("link").asString()).as("연결 직후는 연결 시각부터 센다").isEqualTo("CONNECTED");
        CloseStatus status = watch.closed.poll(6, TimeUnit.SECONDS);
        assertThat(status).isNotNull();
        assertThat(status.getCode()).isEqualTo(WatchHub.HEARTBEAT_TIMEOUT.getCode());
        assertThat(watchItem().path("last_heartbeat_at").isNull()).isTrue();
    }

    @Test
    void 연결_중_토큰을_폐기하면_다음_하트비트에서_닫는다() throws Exception {
        Client watch = connect(watchToken);
        watch.send(heartbeat(watchId, 0, 60, "WIFI"));
        watch.next();

        machineTokens.revoke(MachineKind.DEVICE, watchId);
        watch.send(heartbeat(watchId, 1, 60, "WIFI"));
        CloseStatus status = watch.closed.poll(3, TimeUnit.SECONDS);
        assertThat(status).isNotNull();
        assertThat(status.getCode()).isEqualTo(WatchHub.TOKEN_INVALID.getCode());
        assertThat(watch.poll(200)).as("폐기 뒤에는 답하지 않는다").isNull();
        waitUntil(() -> openConnections() == 0, 2_000);
        assertThat(rows().getFirst().get("end_reason")).isEqualTo("TOKEN_INVALID");
        assertThat(viewerItems()).as("폐기된 워치는 목록에서 빠진다").noneMatch(i -> watchId.equals(i.path("device_id").asString()));
    }

    @Test
    void 같은_워치가_다시_붙으면_옛_연결을_닫는다() throws Exception {
        Client first = connect(watchToken);
        first.send(heartbeat(watchId, 0, 50, "WIFI"));
        first.next();

        Client second = connect(watchToken);
        CloseStatus status = first.closed.poll(3, TimeUnit.SECONDS);
        assertThat(status).isNotNull();
        assertThat(status.getCode()).isEqualTo(WatchHub.REPLACED.getCode());

        second.send(heartbeat(watchId, 0, 49, "WIFI"));
        assertThat(second.next().path("payload").path("heartbeat_seq").asLong()).isZero();
        assertThat(watchItem().path("link").asString()).isEqualTo("CONNECTED");
        List<Map<String, Object>> rows = rows();
        assertThat(rows).hasSize(2);
        assertThat(rows.get(0).get("end_reason")).isEqualTo("REPLACED");
        assertThat(rows.get(0).get("last_heartbeat_at")).as("옛 연결이 받은 마지막 하트비트").isNotNull();
        assertThat(rows.get(1).get("ended_at")).isNull();
    }

    @Test
    void 재시작_때_열린_기록은_마지막으로_살아_있던_때로_닫는다() {
        // 앞 연결의 하트비트 시각(1시간 전)이 있고, 그 뒤 연 연결은 하트비트 없이 EC2 가 죽었다
        jdbc.update("UPDATE device SET last_heartbeat_at = now() - interval '1 hour' WHERE device_id = ?", watchId);
        jdbc.update("INSERT INTO watch_connection (device_id, connected_at) VALUES (?, now() - interval '30 minutes')", watchId);

        assertThat(repository.closeAllOpen()).isGreaterThanOrEqualTo(1);
        Map<String, Object> row = rows().getFirst();
        assertThat(row.get("end_reason")).isEqualTo("EC2_RESTART");
        assertThat(row.get("last_heartbeat_at")).as("앞 연결의 하트비트를 옮기지 않는다").isNull();
        assertThat(row.get("ended_at")).as("기동 시각이 아니라 연결 시각").isEqualTo(row.get("connected_at"));
    }

    @Test
    void 워치가_닫으면_CLOSED로_기록하고_LOST가_된다() throws Exception {
        Client watch = connect(watchToken);
        watch.send(heartbeat(watchId, 0, 40, "WIFI"));
        watch.next();
        watch.close();
        waitUntil(() -> openConnections() == 0, 2_000);
        assertThat(rows().getFirst().get("end_reason")).isEqualTo("CLOSED");
        assertThat(watchItem().path("link").asString()).isEqualTo("LOST");
    }

    @Test
    void source_id가_다르거나_계약을_어긴_하트비트는_버린다() throws Exception {
        Client watch = connect(watchToken);
        watch.send(heartbeat("watch-other", 0, 70, "WIFI"));
        assertThat(watch.poll(500)).as("다른 기기 ID 로 보낸 하트비트").isNull();

        ObjectNode noWatchBlock = heartbeat(watchId, 0, 70, "WIFI");
        ((ObjectNode) noWatchBlock.get("payload")).remove("watch");
        watch.send(noWatchBlock);
        assertThat(watch.poll(500)).as("워치 → EC2 는 watch 블록 필수").isNull();

        watch.sendRaw("not json");
        assertThat(watch.poll(300)).isNull();

        watch.send(heartbeat(watchId, 0, 70, "WIFI"));
        assertThat(watch.next().path("payload").path("echo").path("seq").asLong()).as("틀린 메시지 뒤에도 연결은 산다").isZero();
        assertThat(watch.closed).isEmpty();
    }

    @Test
    void viewer_health_는_등록된_워치_수와_활성_워치_수를_싣는다() throws Exception {
        Client watch = connect(watchToken);
        watch.send(heartbeat(watchId, 0, 90, "CELLULAR"));
        watch.next();

        ObjectNode message = viewerHealth.message();
        assertThat(message.path("schema_version").asString()).isEqualTo("1.3.0");
        assertThat(schemas.violations("messages/viewer_health", message.toString())).isEmpty();
        JsonNode watches = message.get("payload").get("watches");
        assertThat(watches.path("registered").asInt()).isEqualTo(watches.path("items").size());
        int connected = 0;
        List<String> ids = new ArrayList<>();
        for (JsonNode i : watches.path("items")) {
            ids.add(i.path("device_id").asString());
            connected += "CONNECTED".equals(i.path("link").asString()) ? 1 : 0;
        }
        assertThat(watches.path("connected").asInt()).isEqualTo(connected).isPositive();
        assertThat(ids).as("device_id 오름차순, 노트북 제외").isSorted().contains(watchId).doesNotContain(laptopId);
    }

    // ------------------------------------------------------------------ 도우미

    private ObjectNode heartbeat(String sourceId, long seq, int battery, String transport) {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "watch_heartbeat");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", System.currentTimeMillis());
        root.put("source_id", sourceId);
        ObjectNode payload = root.putObject("payload");
        payload.put("heartbeat_seq", seq);
        ObjectNode watch = payload.putObject("watch");
        watch.put("battery_pct", battery);
        watch.put("transport", transport);
        watch.put("app_version", "0.1.0");
        return root;
    }

    private List<JsonNode> viewerItems() {
        List<JsonNode> items = new ArrayList<>();
        viewerHealth.message().get("payload").path("watches").path("items").forEach(items::add);
        return items;
    }

    private JsonNode watchItem() {
        return viewerItems().stream().filter(i -> watchId.equals(i.path("device_id").asString())).findFirst().orElseThrow();
    }

    private List<Map<String, Object>> rows() {
        return jdbc.queryForList("SELECT * FROM watch_connection WHERE device_id = ? ORDER BY connection_id", watchId);
    }

    private int openConnections() {
        Integer n = jdbc.queryForObject("SELECT count(*) FROM watch_connection WHERE device_id = ? AND ended_at IS NULL",
                Integer.class, watchId);
        return n == null ? 0 : n;
    }

    private static void waitUntil(BooleanSupplier condition, long timeoutMs) throws InterruptedException {
        long deadline = System.nanoTime() + timeoutMs * 1_000_000L;
        while (!condition.getAsBoolean() && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
    }

    private Client connect(String token) throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        if (token != null) {
            headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        Client client = new Client(mapper);
        client.session = new StandardWebSocketClient().execute(client.handler, headers,
                URI.create("ws://localhost:" + port + WatchPaths.WS)).get(5, TimeUnit.SECONDS);
        clients.add(client);
        return client;
    }

    private static final class Client {
        final BlockingQueue<String> received = new LinkedBlockingQueue<>();
        final BlockingQueue<CloseStatus> closed = new LinkedBlockingQueue<>();
        final ObjectMapper mapper;
        WebSocketSession session;
        final TextWebSocketHandler handler = new TextWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                received.add(message.getPayload());
            }

            @Override
            public void afterConnectionClosed(WebSocketSession s, CloseStatus status) {
                closed.add(status);
            }
        };

        Client(ObjectMapper mapper) {
            this.mapper = mapper;
        }

        JsonNode next() throws InterruptedException {
            String m = received.poll(5, TimeUnit.SECONDS);
            assertThat(m).as("5초 안에 답이 와야 한다").isNotNull();
            return mapper.readTree(m);
        }

        String poll(long millis) throws InterruptedException {
            return received.poll(millis, TimeUnit.MILLISECONDS);
        }

        void send(JsonNode message) throws Exception {
            sendRaw(mapper.writeValueAsString(message));
        }

        synchronized void sendRaw(String text) throws Exception {
            session.sendMessage(new TextMessage(text));
        }

        void close() {
            try {
                if (session != null && session.isOpen()) {
                    session.close();
                }
            } catch (Exception e) {
                // 이미 닫힘
            }
        }
    }
}
