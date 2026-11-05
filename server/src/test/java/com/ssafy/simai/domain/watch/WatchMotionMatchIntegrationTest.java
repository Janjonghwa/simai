package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import java.net.URI;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
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
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 움직임 대조 배정(S15P31A101-340). 걸음 주기·위상이 다른 세 사람을 world_state(10 Hz, 손목·골반 높이)로 흉내 내고,
 * 각자 찬 시험 워치가 그 사람의 손목 연직 가속도를 0.5~3 Hz 크기 신호로 계약 gait_features(4초 창, 2초마다)에 실어 보낸다.
 * <ul>
 * <li>세 워치가 맞는 사람에게 MOTION_MATCH 로 이어지고 channel_state 가 BOUND, watch_feature 가 남는다</li>
 * <li>관리자 수동 배정(MANUAL)이 걸린 워치·World ID 는 대조가 건드리지 않는다</li>
 * <li>ID 교정(identity_revision 증가)이면 그 대조 배정이 IDENTITY_CHANGED 로 풀리고 UNBOUND</li>
 * <li>서 있는 워치(최대 주파수 없음)는 걸음 근거가 없어 잇지 않는다</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class WatchMotionMatchIntegrationTest {

    private static final double[] FREQ = {1.6, 1.9, 2.2};
    private static final double[] PHASE = {0.0, 1.3, 2.6};
    private static final java.util.concurrent.atomic.AtomicLong NEXT_T0 = new java.util.concurrent.atomic.AtomicLong();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    MachineTokenService machineTokens;

    @Autowired
    WatchBindings bindings;

    @Autowired
    WatchMotionMatcher matcher;

    @Autowired
    ObjectMapper mapper;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final List<AutoCloseable> closeables = new ArrayList<>();
    private final String[] watchIds = new String[3];
    private final String[] tokens = new String[3];
    private final String[] worldIds = new String[3];
    private long adminId;
    private long t0;

    @BeforeEach
    void setUp() {
        for (int i = 0; i < 3; i++) {
            watchIds[i] = "watch-match-" + i + "-" + tag;
            tokens[i] = machineTokens.registerDevice(watchIds[i], "WATCH").token();
            worldIds[i] = "wm-" + i + "-" + tag;
        }
        Long id = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "match-admin-" + tag);
        adminId = id == null ? -1 : id;
        // 시험마다 World 시각을 60초씩 띄운다. 같은 리듬의 사람이 앞 시험에서 같은 시각에 남아 있으면 두 사람을 구별할 수 없어
        // (점수 차이 문턱) 잇지 않는다. 대조는 World 시각으로 '지금'을 보므로 늘 앞으로만 간다
        t0 = NEXT_T0.updateAndGet(prev -> Math.max(System.currentTimeMillis() / 100 * 100, prev + 60_000));
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
        for (String w : watchIds) {
            waitUntil(() -> count("SELECT count(*) FROM watch_connection WHERE device_id = ? AND ended_at IS NULL", w) == 0, 3_000);
            jdbc.update("DELETE FROM watch_binding WHERE device_id = ?", w);
            jdbc.update("DELETE FROM watch_feature WHERE device_id = ?", w);
            jdbc.update("DELETE FROM watch_connection WHERE device_id = ?", w);
            jdbc.update("DELETE FROM device WHERE device_id = ?", w);
        }
        jdbc.update("DELETE FROM channel_state WHERE world_id LIKE ?", "wm-%-" + tag);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 걸음_리듬이_맞는_사람에게_워치가_하나씩_이어지고_BOUND() throws Exception {
        List<Watch> watches = connectAll();
        walk(watches, 0, 16_000, true);
        matcher.matchNow();

        for (int i = 0; i < 3; i++) {
            assertThat(jdbc.queryForList("""
                    SELECT world_id FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL AND method = 'MOTION_MATCH'
                    """, String.class, watchIds[i])).containsExactly(worldIds[i]);
            assertThat(jdbc.queryForObject("""
                    SELECT state FROM channel_state WHERE world_id = ? ORDER BY observed_at DESC LIMIT 1
                    """, String.class, worldIds[i])).isEqualTo("BOUND");
        }
        assertThat(jdbc.queryForObject("SELECT score FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL",
                Double.class, watchIds[0])).isBetween(0.3, 1.0);
        // 창 7개(0~16초, 4초 창 2초마다) × 3 워치
        assertThat(count("SELECT count(*) FROM watch_feature WHERE device_id IN (?, ?, ?)", (Object[]) watchIds)).isEqualTo(21);

        // ID 교정: 0번 사람의 identity_revision 이 오르면 그 대조 배정을 풀고 잠시 잇지 않는다
        matcher.onWorldState(world(t0 + 16_000, 1));
        matcher.matchNow();
        assertThat(jdbc.queryForList("""
                SELECT unbind_reason FROM watch_binding WHERE device_id = ? AND method = 'MOTION_MATCH' ORDER BY binding_id
                """, String.class, watchIds[0])).containsExactly("IDENTITY_CHANGED");
        assertThat(jdbc.queryForObject("""
                SELECT state FROM channel_state WHERE world_id = ? ORDER BY observed_at DESC LIMIT 1
                """, String.class, worldIds[0])).isEqualTo("UNBOUND");
        assertThat(count("SELECT count(*) FROM watch_binding WHERE device_id IN (?, ?) AND unbound_at IS NULL",
                watchIds[1], watchIds[2])).isEqualTo(2);
    }

    @Test
    void 관리자_수동_배정이_걸린_워치와_World_ID_는_대조가_건드리지_않는다() throws Exception {
        // 0번 워치를 1번 사람에게 손으로 잇는다(틀린 짝이지만 관리자 덮어쓰기가 이긴다)
        bindings.bindManually(watchIds[0], worldIds[1], adminId);
        List<Watch> watches = connectAll();
        walk(watches, 0, 16_000, true);
        matcher.matchNow();

        assertThat(jdbc.queryForList("SELECT world_id || ':' || method FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL",
                String.class, watchIds[0])).containsExactly(worldIds[1] + ":MANUAL");
        // 1번 워치의 짝(1번 사람)은 수동 배정에 잡혀 있어 잇지 않고, 2번은 그대로 이어진다
        assertThat(count("SELECT count(*) FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL", watchIds[1])).isZero();
        assertThat(jdbc.queryForList("SELECT world_id FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL",
                String.class, watchIds[2])).containsExactly(worldIds[2]);
    }

    @Test
    void 이어진_뒤_멈춰_서_있어도_배정이_남고_같은_창_재전송과_이상한_창은_점수에_넣지_않는다() throws Exception {
        List<Watch> watches = connectAll();
        walk(watches, 0, 16_000, true);
        matcher.matchNow();
        assertThat(count("SELECT count(*) FROM watch_binding WHERE device_id IN (?, ?, ?) AND unbound_at IS NULL",
                (Object[]) watchIds)).isEqualTo(3);

        // 20초 동안 셋 다 멈춰 서서 일한다(근거 없는 창). 반대 근거가 아니라 배정이 풀리지 않아야 한다
        walk(watches, 16_000, 36_000, false);
        matcher.matchNow();
        for (int i = 0; i < 3; i++) {
            assertThat(jdbc.queryForList("SELECT world_id FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL",
                    String.class, watchIds[i])).containsExactly(worldIds[i]);
        }

        // 같은 창 재전송은 저장도 점수도 한 번, 끝이 시작보다 앞선 창은 버린다
        int features = count("SELECT count(*) FROM watch_feature WHERE device_id = ?", watchIds[0]);
        watches.get(0).send(gait(0, t0, true));
        ObjectNode broken = gait(0, t0 + 40_000, true);
        ((ObjectNode) broken.get("payload")).put("window_end_ms", t0 + 39_000);
        watches.get(0).send(broken);
        Thread.sleep(500);
        matcher.matchNow();
        assertThat(count("SELECT count(*) FROM watch_feature WHERE device_id = ?", watchIds[0])).isEqualTo(features);
        assertThat(jdbc.queryForList("SELECT world_id FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL",
                String.class, watchIds[0])).containsExactly(worldIds[0]);
    }

    @Test
    void 서_있는_워치는_걸음_근거가_없어_잇지_않는다() throws Exception {
        List<Watch> watches = connectAll();
        walk(watches, 0, 16_000, false);
        matcher.matchNow();
        assertThat(count("SELECT count(*) FROM watch_binding WHERE device_id IN (?, ?, ?) AND unbound_at IS NULL",
                (Object[]) watchIds)).isZero();
        assertThat(jdbc.queryForObject("""
                SELECT state FROM channel_state WHERE world_id = ? ORDER BY observed_at DESC LIMIT 1
                """, String.class, worldIds[0])).isEqualTo("UNBOUND");
    }

    // ------------------------------------------------------------------ 흉내

    /** [from, to) 를 world_state 10 Hz 로 흘리고, 창마다 워치가 gait_features 를 보낸다. 모든 창이 저장될 때까지 기다린다 */
    private void walk(List<Watch> watches, long from, long to, boolean walking) throws Exception {
        int before = count("SELECT count(*) FROM watch_feature WHERE device_id IN (?, ?, ?)", (Object[]) watchIds);
        for (long t = from; t < to; t += 100) {
            matcher.onWorldState(world(t0 + t, 0, walking));
        }
        int windows = 0;
        for (long start = from; start + 4_000 <= to; start += 2_000) {
            windows++;
            for (int i = 0; i < 3; i++) {
                watches.get(i).send(gait(i, t0 + start, walking));
            }
        }
        int expected = before + windows * 3;
        waitUntil(() -> count("SELECT count(*) FROM watch_feature WHERE device_id IN (?, ?, ?)", (Object[]) watchIds) >= expected,
                5_000);
    }

    private WorldStateStored world(long t, int revisionOfFirst) {
        return world(t, revisionOfFirst, true);
    }

    /** moving=false 면 셋 다 멈춰 선다(속력 0, 손목 높이 고정) */
    private WorldStateStored world(long t, int revisionOfFirst, boolean moving) {
        ObjectNode p = mapper.createObjectNode();
        p.put("t_latest_global", t);
        ArrayNode tracks = p.putArray("tracks");
        double sec = (t - t0) / 1000.0;
        for (int i = 0; i < 3; i++) {
            double w = 2 * Math.PI * FREQ[i];
            // 손목 연직 가속도가 −sin(ωt+φ) 가 되게: z = sin(ωt+φ) / ω²
            double wristZ = moving ? 1.0 + Math.sin(w * sec + PHASE[i]) / (w * w) : 1.0;
            ObjectNode tr = tracks.addObject();
            tr.put("world_id", worldIds[i]);
            tr.put("track_lifecycle", "CONFIRMED");
            tr.put("identity_revision", i == 0 ? revisionOfFirst : 0);
            tr.putArray("velocity").add(moving ? 1.0 : 0.0).add(0.0).add(0.0);
            tr.putArray("pelvis_xyz").add(i).add(sec).add(0.95);
            ObjectNode sk = tr.putObject("skeleton");
            sk.put("joint_layout", "COCO17");
            ArrayNode joints = sk.putArray("joints");
            ArrayNode validity = sk.putArray("validity");
            ArrayNode source = sk.putArray("joint_source");
            for (int j = 0; j < 17; j++) {
                joints.addArray().add(i).add(sec).add(j == 9 || j == 10 ? wristZ : 1.2);
                validity.add("VALID");
                source.add("observed");
            }
        }
        return new WorldStateStored(new I1Envelope("2.0.0", 2, "world_state", UUID.randomUUID(), Instant.ofEpochMilli(t),
                "tier0-test", p));
    }

    /** i 번 사람의 손목 연직 가속도(−sin, 크기 1.2)를 4초 창 10 Hz 로. walking=false 면 거의 평평하고 최대 주파수 없음 */
    private ObjectNode gait(int i, long start, boolean walking) {
        ObjectNode root = envelope("gait_features", watchIds[i]);
        ObjectNode p = root.putObject("payload");
        p.put("clock_basis", "WATCH");
        p.put("clock_offset_ms", 0);
        p.put("window_start_ms", start);
        p.put("window_end_ms", start + 4_000);
        p.put("sample_rate_hz", 50);
        ArrayNode steps = p.putArray("step_times_ms");
        ObjectNode band = p.putObject("band_magnitude");
        band.put("rate_hz", 10);
        ArrayNode values = band.putArray("values");
        double w = 2 * Math.PI * FREQ[i];
        double sumSq = 0;
        for (int k = 0; k < 40; k++) {
            double sec = (start - t0) / 1000.0 + k / 10.0;
            double v = walking ? -1.2 * Math.sin(w * sec + PHASE[i]) : 0.01 * Math.sin(k);
            values.add(Math.round(v * 1000) / 1000.0);
            sumSq += v * v;
        }
        if (walking) {
            for (long s = start + 200; s < start + 4_000; s += Math.round(1000 / FREQ[i])) {
                steps.add(s);
            }
        }
        band.put("rms", Math.round(Math.sqrt(sumSq / 40) * 1000) / 1000.0);
        if (walking) {
            band.put("peak_freq_hz", FREQ[i]);
        } else {
            band.putNull("peak_freq_hz");
        }
        return root;
    }

    private ObjectNode envelope(String msgType, String sourceId) {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", msgType);
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", System.currentTimeMillis());
        root.put("source_id", sourceId);
        return root;
    }

    private List<Watch> connectAll() throws Exception {
        List<Watch> out = new ArrayList<>();
        for (int i = 0; i < 3; i++) {
            out.add(watch(watchIds[i], tokens[i]));
        }
        return out;
    }

    /** 붙고 첫 하트비트 답을 받은 워치. 0.5초마다 하트비트를 보내 끊김(시험 3초)에 걸리지 않는다 */
    private Watch watch(String deviceId, String token) throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        Watch watch = new Watch(mapper);
        watch.session = new StandardWebSocketClient().execute(watch.handler, headers,
                URI.create("ws://localhost:" + port + WatchPaths.WS)).get(5, TimeUnit.SECONDS);
        closeables.add(watch);
        watch.send(heartbeat(deviceId));
        watch.awaitHeartbeat();
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

    private ObjectNode heartbeat(String sourceId) {
        ObjectNode root = envelope("watch_heartbeat", sourceId);
        ObjectNode p = root.putObject("payload");
        p.put("heartbeat_seq", 0);
        ObjectNode w = p.putObject("watch");
        w.put("battery_pct", 80);
        w.put("transport", "WIFI");
        return root;
    }

    private int count(String sql, Object... args) {
        Integer n = jdbc.queryForObject(sql, Integer.class, args);
        return n == null ? 0 : n;
    }

    private static void waitUntil(BooleanSupplier condition, long timeoutMs) throws InterruptedException {
        long deadline = System.nanoTime() + timeoutMs * 1_000_000L;
        while (!condition.getAsBoolean() && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
    }

    private static final class Watch implements AutoCloseable {
        final BlockingQueue<JsonNode> received = new LinkedBlockingQueue<>();
        final ObjectMapper mapper;
        WebSocketSession session;
        final TextWebSocketHandler handler = new TextWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                JsonNode m = mapper.readTree(message.getPayload());
                // 연결 때 붙는 배정 알림(-510)은 이 시험의 관심사가 아니다
                if (!"watch_binding_notice".equals(m.path("msg_type").asString())) {
                    received.add(m);
                }
            }
        };

        Watch(ObjectMapper mapper) {
            this.mapper = mapper;
        }

        void awaitHeartbeat() throws InterruptedException {
            JsonNode m = received.poll(5, TimeUnit.SECONDS);
            assertThat(m).as("첫 하트비트 답").isNotNull();
            assertThat(m.path("msg_type").asString()).isEqualTo("watch_heartbeat");
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
