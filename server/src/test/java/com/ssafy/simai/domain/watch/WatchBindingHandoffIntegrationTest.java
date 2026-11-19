package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import java.net.URI;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
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
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 워치 배정 자리 이어받기(S15P31A101-793). 배정된 사람의 트랙이 사라지고(가림) 그 자리에 새 ID 가 나타나는 것을 world_state 로 흉내 낸다.
 * <ul>
 * <li>사라진 자리 1.5 m 안에 새 트랙 하나만 나타나면 수동 배정이 새 ID 로 옮겨지고(임시, handoff_from), 옛 행은 HANDOFF 로 닫힌다</li>
 * <li>새 트랙이 둘이거나 멀거나 옛 자리에 배정 없는 다른 사람이 있으면 옮기지 않는다</li>
 * <li>임시 배정은 워치가 걷는데 트랙이 서 있는 반대 근거가 쌓이면 MATCH_LOST 로 풀리고, 걸음이 맞으면 확정된다(수동 배정 그대로)</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class WatchBindingHandoffIntegrationTest {

    private static final int FEED_BATCH = WatchMotionMatcher.MAX_PENDING_WORLD / 2;

    /** 흉내 낸 사람: 자리(x, y)와 걸음(없으면 서 있음). 걷는 손목 연직 가속도는 −sin(2πf·t + φ) */
    private record Person(String worldId, double x, double y, Double freq, double phase) {
        static Person still(String worldId, double x, double y) {
            return new Person(worldId, x, y, null, 0);
        }

        static Person walking(String worldId, double x, double y, double freq, double phase) {
            return new Person(worldId, x, y, freq, phase);
        }
    }

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
    private String watchA;
    private String watchB;
    private String tokenA;
    private String tokenB;
    private long adminId;
    private long t0;

    @BeforeEach
    void setUp() {
        watchA = "watch-ho-a-" + tag;
        watchB = "watch-ho-b-" + tag;
        tokenA = machineTokens.registerDevice(watchA, "WATCH").token();
        tokenB = machineTokens.registerDevice(watchB, "WATCH").token();
        Long id = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "handoff-admin-" + tag);
        adminId = id == null ? -1 : id;
        t0 = WatchMatchTestTimes.nextT0();
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
        for (String w : List.of(watchA, watchB)) {
            waitUntil(() -> count("SELECT count(*) FROM watch_connection WHERE device_id = ? AND ended_at IS NULL", w) == 0, 3_000);
            jdbc.update("DELETE FROM watch_binding WHERE device_id = ?", w);
            jdbc.update("DELETE FROM watch_feature WHERE device_id = ?", w);
            jdbc.update("DELETE FROM watch_connection WHERE device_id = ?", w);
            jdbc.update("DELETE FROM device WHERE device_id = ?", w);
        }
        String like = "wh-%-" + tag;
        jdbc.update("DELETE FROM channel_state WHERE world_id LIKE ?", like);
        jdbc.update("DELETE FROM person_world_merge_log WHERE old_world_id LIKE ? OR survivor_world_id LIKE ?", like, like);
        jdbc.update("DELETE FROM person_unidentified_number_history WHERE world_id LIKE ?", like);
        jdbc.update("DELETE FROM person_world_name_history WHERE world_id LIKE ?", like);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 배정된_트랙이_사라진_자리에_새_트랙_하나가_나타나면_배정이_임시로_옮겨진다() throws Exception {
        String a = world("A");
        String b = world("B");
        bindings.bindManually(watchA, a, adminId);
        feed(0, 1_000, Person.still(a, 0, 0));
        matcher.matchNow();

        // 가려졌다 0.5 m 옆에서 새 ID 로 나온다. 처음 두 프레임은 자리가 없다(position_xy null): 자리를 안 프레임부터 센다
        feed(1_000, 1_200, Person.still(b, Double.NaN, Double.NaN));
        assertThat(activeWorld(watchA)).isEqualTo(a);
        feed(1_200, 1_700, Person.still(b, 0.5, 0.2));

        Map<String, Object> moved = jdbc.queryForMap("""
                SELECT world_id, method, bound_by_user_id, handoff_from, handoff_confirmed_at
                FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL
                """, watchA);
        assertThat(moved.get("world_id")).isEqualTo(b);
        assertThat(moved.get("method")).isEqualTo("MANUAL");
        assertThat(((Number) moved.get("bound_by_user_id")).longValue()).isEqualTo(adminId);
        assertThat(moved.get("handoff_from")).isEqualTo(a);
        assertThat(moved.get("handoff_confirmed_at")).isNull();
        assertThat(jdbc.queryForObject("""
                SELECT unbind_reason FROM watch_binding WHERE device_id = ? AND world_id = ? AND unbound_at IS NOT NULL
                """, String.class, watchA, a)).isEqualTo("HANDOFF");
        matcher.matchNow();
        assertThat(jdbc.queryForObject("""
                SELECT state FROM channel_state WHERE world_id = ? ORDER BY observed_at DESC LIMIT 1
                """, String.class, b)).isEqualTo("LOST"); // 워치는 붙이지 않았다: 배정은 있고 연결이 없다
    }

    @Test
    void 새_트랙이_둘이거나_멀거나_옛_자리에_다른_사람이_있으면_옮기지_않는다() throws Exception {
        String a = world("A");
        bindings.bindManually(watchA, a, adminId);

        // 1) 같은 자리에 새 ID 둘: 누구인지 모른다
        feed(0, 1_000, Person.still(a, 0, 0));
        matcher.matchNow();
        feed(1_000, 1_500, Person.still(world("B"), 0.3, 0), Person.still(world("C"), -0.3, 0.4));
        assertThat(activeWorld(watchA)).isEqualTo(a);

        // 2) 멀리서 나온 새 ID: 다른 사람이다(A 는 5초 창 안이라 아직 후보)
        feed(1_500, 2_000, Person.still(world("D"), 3.0, 0));
        assertThat(activeWorld(watchA)).isEqualTo(a);

        // 3) 옛 자리 근처에 배정 없는 사람 E 가 전부터 서 있었다: A 가 사라지고 F 가 나와도 E 일 수 있어 옮기지 않는다
        feed(6_000, 7_000, Person.still(a, 10, 10), Person.still(world("E"), 10.6, 10));
        matcher.matchNow();
        feed(7_000, 7_500, Person.still(world("E"), 10.6, 10), Person.still(world("F"), 10.2, 10.3));
        assertThat(activeWorld(watchA)).isEqualTo(a);
        assertThat(count("SELECT count(*) FROM watch_binding WHERE device_id = ? AND handoff_from IS NOT NULL", watchA)).isZero();
    }

    @Test
    void 임시_배정은_반대_근거가_쌓이면_풀리고_걸음이_맞으면_확정된다() throws Exception {
        Watch wa = connect(watchA, tokenA);
        Watch wb = connect(watchB, tokenB);
        String a = world("A");
        String b = world("B");
        String c = world("C");
        String d = world("D");
        bindings.bindManually(watchA, a, adminId);
        bindings.bindManually(watchB, c, adminId);
        Person cWalking = Person.walking(c, 5, 5, 1.9, 1.3);
        feed(0, 1_000, Person.still(a, 0, 0), cWalking);
        matcher.matchNow();

        // A → B(서 있음), C → D(같은 걸음으로 계속 걸음)로 각각 자리 이어받기
        Person dWalking = Person.walking(d, 5.3, 5, 1.9, 1.3);
        feed(1_000, 2_000, Person.still(b, 0.4, 0.1), dWalking);
        assertThat(activeWorld(watchA)).isEqualTo(b);
        assertThat(activeWorld(watchB)).isEqualTo(d);

        // 워치 A 는 걷는 창을 보내는데 B 는 서 있다(반대 근거). 워치 B 의 창은 D 의 손목과 같은 리듬이다(같은 근거)
        for (long start = 2_000; start + 4_000 <= 16_000; start += 2_000) {
            wa.send(gait(watchA, t0 + start, 1.6, 0.0, true));
            wb.send(gait(watchB, t0 + start, 1.9, 1.3, true));
        }
        feed(2_000, 16_000, Person.still(b, 0.4, 0.1), dWalking);
        assertThat(waitUntil(() -> count("SELECT count(*) FROM watch_feature WHERE device_id IN (?, ?)", watchA, watchB) >= 12,
                30_000)).as("걸음 특징 창 저장").isTrue();
        matcher.matchNow();

        assertThat(jdbc.queryForList("""
                SELECT unbind_reason FROM watch_binding WHERE device_id = ? AND world_id = ? ORDER BY binding_id
                """, String.class, watchA, b)).containsExactly("MATCH_LOST");
        assertThat(count("SELECT count(*) FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL", watchA)).isZero();

        Map<String, Object> confirmed = jdbc.queryForMap("""
                SELECT world_id, method, handoff_from, handoff_confirmed_at FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL
                """, watchB);
        assertThat(confirmed.get("world_id")).isEqualTo(d);
        assertThat(confirmed.get("method")).isEqualTo("MANUAL");
        assertThat(confirmed.get("handoff_from")).isEqualTo(c);
        assertThat(confirmed.get("handoff_confirmed_at")).isNotNull();
    }

    // ------------------------------------------------------------------ 흉내

    private String world(String name) {
        return "wh-" + name + "-" + tag;
    }

    private String activeWorld(String deviceId) {
        return bindings.activeForDevice(deviceId).map(WatchBindings.Binding::worldId).orElse(null);
    }

    /** [from, to) 를 world_state 10 Hz 로 흘리고 대조 스레드가 다 처리할 때까지 기다린다 */
    private void feed(long from, long to, Person... people) throws Exception {
        int fed = 0;
        for (long t = from; t < to; t += 100) {
            matcher.onWorldState(worldState(t0 + t, people));
            if (++fed % FEED_BATCH == 0) {
                matcher.awaitInputs();
            }
        }
        matcher.awaitInputs();
    }

    private WorldStateStored worldState(long t, Person... people) {
        ObjectNode p = mapper.createObjectNode();
        p.put("t_latest_global", t);
        ArrayNode tracks = p.putArray("tracks");
        double sec = (t - t0) / 1000.0;
        for (Person person : people) {
            boolean moving = person.freq() != null;
            double w = moving ? 2 * Math.PI * person.freq() : 0;
            // 손목 연직 가속도가 −sin(ωt+φ) 가 되게: z = sin(ωt+φ) / ω²
            double wristZ = moving ? 1.0 + Math.sin(w * sec + person.phase()) / (w * w) : 1.0;
            ObjectNode tr = tracks.addObject();
            tr.put("world_id", person.worldId());
            tr.put("track_lifecycle", "CONFIRMED");
            tr.put("identity_revision", 0);
            boolean placed = !Double.isNaN(person.x());
            if (placed) {
                tr.putArray("position_xy").add(person.x()).add(person.y());
            } else {
                tr.putArray("position_xy").addNull().addNull();
            }
            double px = placed ? person.x() : 0;
            double py = placed ? person.y() : 0;
            tr.putArray("velocity").add(moving ? 1.0 : 0.0).add(0.0).add(0.0);
            tr.putArray("pelvis_xyz").add(px).add(py).add(0.95);
            ObjectNode sk = tr.putObject("skeleton");
            sk.put("joint_layout", "COCO17");
            ArrayNode joints = sk.putArray("joints");
            ArrayNode validity = sk.putArray("validity");
            ArrayNode source = sk.putArray("joint_source");
            for (int j = 0; j < 17; j++) {
                joints.addArray().add(px).add(py).add(j == 9 || j == 10 ? wristZ : 1.2);
                validity.add("VALID");
                source.add("observed");
            }
        }
        return new WorldStateStored(new I1Envelope("2.0.0", 2, "world_state", UUID.randomUUID(), Instant.ofEpochMilli(t),
                "tier0-test", p));
    }

    /** 손목 연직 가속도(−sin, 크기 1.2)를 4초 창 10 Hz 로 */
    private ObjectNode gait(String deviceId, long start, double freq, double phase, boolean walking) {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "gait_features");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", System.currentTimeMillis());
        root.put("source_id", deviceId);
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
        double w = 2 * Math.PI * freq;
        double sumSq = 0;
        for (int k = 0; k < 40; k++) {
            double sec = (start - t0) / 1000.0 + k / 10.0;
            double v = walking ? -1.2 * Math.sin(w * sec + phase) : 0.01 * Math.sin(k);
            values.add(Math.round(v * 1000) / 1000.0);
            sumSq += v * v;
        }
        if (walking) {
            for (long s = start + 200; s < start + 4_000; s += Math.round(1000 / freq)) {
                steps.add(s);
            }
        }
        band.put("rms", Math.round(Math.sqrt(sumSq / 40) * 1000) / 1000.0);
        if (walking) {
            band.put("peak_freq_hz", freq);
        } else {
            band.putNull("peak_freq_hz");
        }
        return root;
    }

    /** 붙고 연결 기록이 생긴 워치. 0.5초마다 하트비트를 보내 끊김(시험 3초)에 걸리지 않는다 */
    private Watch connect(String deviceId, String token) throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        Watch watch = new Watch(mapper);
        watch.session = new StandardWebSocketClient().execute(watch.handler, headers,
                URI.create("ws://localhost:" + port + WatchPaths.WS)).get(5, TimeUnit.SECONDS);
        closeables.add(watch);
        watch.send(heartbeat(deviceId));
        assertThat(waitUntil(() -> count("SELECT count(*) FROM watch_connection WHERE device_id = ? AND ended_at IS NULL", deviceId) > 0,
                5_000)).as("워치 연결").isTrue();
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

    private int count(String sql, Object... args) {
        Integer n = jdbc.queryForObject(sql, Integer.class, args);
        return n == null ? 0 : n;
    }

    private static boolean waitUntil(BooleanSupplier condition, long timeoutMs) throws InterruptedException {
        long deadline = System.nanoTime() + timeoutMs * 1_000_000L;
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() >= deadline) {
                return false;
            }
            Thread.sleep(50);
        }
        return true;
    }

    private static final class Watch implements AutoCloseable {
        final ObjectMapper mapper;
        WebSocketSession session;
        final TextWebSocketHandler handler = new TextWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                // 하트비트 답·배정 알림은 이 시험의 관심사가 아니다
            }
        };

        Watch(ObjectMapper mapper) {
            this.mapper = mapper;
        }

        synchronized void send(ObjectNode message) throws Exception {
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
