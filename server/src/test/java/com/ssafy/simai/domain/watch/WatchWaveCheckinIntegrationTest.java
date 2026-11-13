package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import java.net.URI;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
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
 * 손 흔들기 체크인과 ID 따라가기(S15P31A101-508). 서 있는 세 사람(속력 0, 골반 고정)의 손목을 world_state(10 Hz, 3D, 잡음 5 mm)로
 * 흉내 내고, 흔드는 사람의 워치는 손목 가속도 크기 |g + a| 를 대역 통과한 신호를 계약 gait_features 로 보낸다.
 * <ul>
 * <li>세 명이 서 있고 한 명만 흔들면 그 워치만 그 사람에게 이어진다(나머지 워치는 조용해 잇지 않는다)</li>
 * <li>두 명이 동시에 다른 리듬으로 흔들면 각자 맞게 이어진다</li>
 * <li>같은 리듬으로 동시에 흔들면 구별할 수 없어 잇지 않는다</li>
 * <li>world_state aliases(병합)로 옛 ID 의 배정(대조·수동)이 살아남은 ID 로 옮겨지고 옛 행은 ID_MERGED 로 닫힌다</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class WatchWaveCheckinIntegrationTest {

    /** world_state 를 이만큼 넣을 때마다 대조 스레드가 처리하길 기다린다(대기열 상한의 절반) */
    private static final int FEED_BATCH = WatchMotionMatcher.MAX_PENDING_WORLD / 2;
    /** 손 흔들기 진폭 벡터(m): 사람마다 방향이 다르다 */
    private static final double[][] AMP = {{0.10, 0.04, 0.08}, {0.03, 0.12, 0.06}, {0.09, 0.09, 0.0}};
    private static final double[] PHASE = {0.0, 1.1, 2.3};

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
            watchIds[i] = "watch-wave-" + i + "-" + tag;
            tokens[i] = machineTokens.registerDevice(watchIds[i], "WATCH").token();
            worldIds[i] = "ww-" + i + "-" + tag;
        }
        Long id = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "wave-admin-" + tag);
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
        for (String w : watchIds) {
            waitUntil(() -> count("SELECT count(*) FROM watch_connection WHERE device_id = ? AND ended_at IS NULL", w) == 0, 3_000);
            jdbc.update("DELETE FROM watch_binding WHERE device_id = ?", w);
            jdbc.update("DELETE FROM watch_feature WHERE device_id = ?", w);
            jdbc.update("DELETE FROM watch_connection WHERE device_id = ?", w);
            jdbc.update("DELETE FROM device WHERE device_id = ?", w);
        }
        jdbc.update("DELETE FROM channel_state WHERE world_id LIKE ?", "ww%-" + tag);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 서_있는_세_사람_중_한_명만_흔들면_그_워치만_이어진다() throws Exception {
        List<Watch> watches = connectAll();
        run(watches, 0, 16_000, new double[] {2.0, 0, 0}, new String[] {null, null, null});
        matcher.matchNow();

        assertThat(activeWorld(watchIds[0])).containsExactly(worldIds[0]);
        assertThat(activeWorld(watchIds[1])).isEmpty();
        assertThat(activeWorld(watchIds[2])).isEmpty();
        assertThat(jdbc.queryForObject("SELECT method FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL",
                String.class, watchIds[0])).isEqualTo("MOTION_MATCH");
    }

    @Test
    void 두_사람이_동시에_다른_리듬으로_흔들면_각자_맞게_이어진다() throws Exception {
        List<Watch> watches = connectAll();
        run(watches, 0, 20_000, new double[] {1.7, 2.6, 0}, new String[] {null, null, null});
        matcher.matchNow();

        assertThat(activeWorld(watchIds[0])).containsExactly(worldIds[0]);
        assertThat(activeWorld(watchIds[1])).containsExactly(worldIds[1]);
        assertThat(activeWorld(watchIds[2])).isEmpty();
    }

    @Test
    void 같은_리듬으로_동시에_흔들면_구별할_수_없어_잇지_않는다() throws Exception {
        List<Watch> watches = connectAll();
        // 같은 주파수·같은 위상(진폭 방향만 다르다). 워치 신호가 같아 두 사람 점수가 거의 같다
        run(watches, 0, 16_000, new double[] {2.0, 2.0, 0}, new String[] {null, null, null}, true);
        matcher.matchNow();

        assertThat(activeWorld(watchIds[0])).isEmpty();
        assertThat(activeWorld(watchIds[1])).isEmpty();
    }

    @Test
    void 병합되면_대조_배정이_살아남은_ID_로_옮겨지고_옛_행은_ID_MERGED() throws Exception {
        List<Watch> watches = connectAll();
        run(watches, 0, 16_000, new double[] {2.0, 0, 0}, new String[] {null, null, null});
        matcher.matchNow();
        assertThat(activeWorld(watchIds[0])).containsExactly(worldIds[0]);

        // 0번 사람이 새 ID 로 합쳐졌다: 살아남은 ID 가 옛 ID 를 aliases 로 단다
        String survivor = "ww-merged-" + tag;
        matcher.onWorldState(mergedWorld(t0 + 16_000, survivor, worldIds[0]));
        matcher.matchNow();

        assertThat(activeWorld(watchIds[0])).containsExactly(survivor);
        assertThat(jdbc.queryForList("""
                SELECT method || ':' || coalesce(unbind_reason, '-') FROM watch_binding WHERE device_id = ? ORDER BY binding_id
                """, String.class, watchIds[0])).containsExactly("MOTION_MATCH:ID_MERGED", "MOTION_MATCH:-");
        // 같은 병합이 aliases 에 계속 실려도 또 옮기지 않는다
        matcher.onWorldState(mergedWorld(t0 + 16_100, survivor, worldIds[0]));
        matcher.matchNow();
        assertThat(count("SELECT count(*) FROM watch_binding WHERE device_id = ?", watchIds[0])).isEqualTo(2);
        // 옮긴 배정은 대조가 풀지 않는다(점수를 같이 옮겼고 의심 구간이 없다)
        assertThat(activeWorld(watchIds[0])).containsExactly(survivor);
    }

    @Test
    void 병합되면_수동_배정도_옮겨지고_배정자를_이어받는다() throws Exception {
        bindings.bindManually(watchIds[1], worldIds[1], adminId);
        String survivor = "ww-merged-" + tag;
        matcher.onWorldState(mergedWorld(t0, survivor, worldIds[1]));
        matcher.matchNow();

        assertThat(jdbc.queryForList("""
                SELECT world_id || ':' || method || ':' || bound_by_user_id FROM watch_binding
                WHERE device_id = ? AND unbound_at IS NULL
                """, String.class, watchIds[1])).containsExactly(survivor + ":MANUAL:" + adminId);
        assertThat(jdbc.queryForList("""
                SELECT unbind_reason FROM watch_binding WHERE device_id = ? AND unbound_at IS NOT NULL
                """, String.class, watchIds[1])).containsExactly("ID_MERGED");
    }

    @Test
    void 병합_충돌에서는_수동이_이기고_둘_다_대조면_옮기지_않는다() throws Exception {
        String old = "ww-old-" + tag;
        String survivor = "ww-surv-" + tag;
        // 옛 ID 는 수동(0번 워치), 살아남은 ID 는 대조(1번 워치): 수동이 옮겨지고 대조는 REPLACED
        bindings.bindManually(watchIds[0], old, adminId);
        assertThat(bindings.bindByMotion(watchIds[1], survivor, 0.8)).isTrue();
        assertThat(bindings.followMerge(old, survivor)).isNotNull();
        assertThat(activeWorld(watchIds[0])).containsExactly(survivor);
        assertThat(activeWorld(watchIds[1])).isEmpty();
        assertThat(jdbc.queryForList("SELECT unbind_reason FROM watch_binding WHERE device_id = ? ORDER BY binding_id",
                String.class, watchIds[1])).containsExactly("REPLACED");

        // 살아남은 ID 가 이미 수동이면 그대로, 옛 수동은 ID_MERGED 로 닫는다
        String old2 = "ww-old2-" + tag;
        bindings.bindManually(watchIds[2], old2, adminId);
        assertThat(bindings.followMerge(old2, survivor)).isNull();
        assertThat(activeWorld(watchIds[0])).containsExactly(survivor);
        assertThat(activeWorld(watchIds[2])).isEmpty();

        // 둘 다 대조면 옮기지 않는다(호출자가 둘 다 푼다)
        jdbc.update("DELETE FROM watch_binding WHERE device_id IN (?, ?, ?)", (Object[]) watchIds);
        assertThat(bindings.bindByMotion(watchIds[0], "ww-a-" + tag, 0.8)).isTrue();
        assertThat(bindings.bindByMotion(watchIds[1], "ww-b-" + tag, 0.8)).isTrue();
        assertThat(bindings.followMerge("ww-a-" + tag, "ww-b-" + tag)).isNull();
        assertThat(count("SELECT count(*) FROM watch_binding WHERE device_id IN (?, ?) AND unbound_at IS NULL", watchIds[0],
                watchIds[1])).isEqualTo(2);
    }

    // ------------------------------------------------------------------ 흉내

    private List<String> activeWorld(String device) {
        return jdbc.queryForList("SELECT world_id FROM watch_binding WHERE device_id = ? AND unbound_at IS NULL", String.class, device);
    }

    /** freq[i] > 0 이면 i 번 사람이 그 주파수로 손을 흔든다. 전원 서 있다 */
    private void run(List<Watch> watches, long from, long to, double[] freq, String[] unused) throws Exception {
        run(watches, from, to, freq, unused, false);
    }

    private void run(List<Watch> watches, long from, long to, double[] freq, String[] unused, boolean samePhase) throws Exception {
        int before = count("SELECT count(*) FROM watch_feature WHERE device_id IN (?, ?, ?)", (Object[]) watchIds);
        Random noise = new Random(7);
        int fed = 0;
        for (long t = from; t < to; t += 100) {
            matcher.onWorldState(world(t0 + t, freq, samePhase, noise));
            // 실제로는 10 Hz 로 들어온다. 몇 초 분량을 한꺼번에 넣으면 대조 스레드가 밀린 사이(주기 대조의 DB 쓰기, 부하)
            // 쌓인 것이 MAX_PENDING_WORLD 를 넘어 버려져 손목 표본이 빈다(-674). 절반마다 처리될 때까지 기다린다
            if (++fed % FEED_BATCH == 0) {
                matcher.awaitInputs();
            }
        }
        int windows = 0;
        for (long start = from; start + 4_000 <= to; start += 2_000) {
            windows++;
            for (int i = 0; i < 3; i++) {
                watches.get(i).send(gait(i, t0 + start, freq[i], samePhase));
            }
        }
        int expected = before + windows * 3;
        // 창이 다 저장되기 전에 대조하면 점수가 모자라 엉뚱하게 실패한다. 조건 대기라 상한은 넉넉히 둔다
        assertThat(waitUntil(() -> count("SELECT count(*) FROM watch_feature WHERE device_id IN (?, ?, ?)",
                (Object[]) watchIds) >= expected, 30_000)).as("워치 특징 창 %d 개 저장", expected).isTrue();
    }

    private double phase(int i, boolean samePhase) {
        return samePhase ? 0.0 : PHASE[i];
    }

    private WorldStateStored world(long t, double[] freq, boolean samePhase, Random noise) {
        ObjectNode p = mapper.createObjectNode();
        p.put("t_latest_global", t);
        ArrayNode tracks = p.putArray("tracks");
        double sec = (t - t0) / 1000.0;
        for (int i = 0; i < 3; i++) {
            ObjectNode tr = tracks.addObject();
            tr.put("world_id", worldIds[i]);
            tr.put("track_lifecycle", "CONFIRMED");
            tr.put("identity_revision", 0);
            tr.putArray("velocity").add(0.0).add(0.0).add(0.0);
            tr.putArray("pelvis_xyz").add(i * 2.0).add(0.0).add(0.95 + noise.nextGaussian() * 0.005);
            ObjectNode sk = tr.putObject("skeleton");
            sk.put("joint_layout", "COCO17");
            ArrayNode joints = sk.putArray("joints");
            ArrayNode validity = sk.putArray("validity");
            ArrayNode source = sk.putArray("joint_source");
            for (int j = 0; j < 17; j++) {
                double x = i * 2.0;
                double y = 0.0;
                double z = 1.2;
                if (j == 10 && freq[i] > 0) { // 오른 손목만 흔든다
                    double w = 2 * Math.PI * freq[i];
                    double s = Math.sin(w * sec + phase(i, samePhase));
                    x += AMP[i][0] * s;
                    y += AMP[i][1] * s;
                    z += 0.3 + AMP[i][2] * s;
                }
                joints.addArray().add(x + noise.nextGaussian() * 0.005).add(y + noise.nextGaussian() * 0.005)
                        .add(z + noise.nextGaussian() * 0.005);
                validity.add("VALID");
                source.add("observed");
            }
        }
        return new WorldStateStored(new I1Envelope("2.0.0", 2, "world_state", UUID.randomUUID(), Instant.ofEpochMilli(t),
                "tier0-test", p));
    }

    /** 병합 뒤 한 프레임: 살아남은 ID 하나(aliases 에 옛 ID), 서 있다. 손목은 안 움직인다 */
    private WorldStateStored mergedWorld(long t, String survivor, String oldId) {
        ObjectNode p = mapper.createObjectNode();
        p.put("t_latest_global", t);
        ArrayNode tracks = p.putArray("tracks");
        ObjectNode tr = tracks.addObject();
        tr.put("world_id", survivor);
        tr.put("track_lifecycle", "CONFIRMED");
        tr.put("identity_revision", 1);
        tr.putArray("aliases").add(oldId);
        tr.putArray("velocity").add(0.0).add(0.0).add(0.0);
        tr.putArray("pelvis_xyz").add(0.0).add(0.0).add(0.95);
        return new WorldStateStored(new I1Envelope("2.0.0", 2, "world_state", UUID.randomUUID(), Instant.ofEpochMilli(t),
                "tier0-test", p));
    }

    /** 흔드는 사람(freq > 0)의 워치: 손목 |g + a| 를 10 Hz 로 만들어 대역 통과. 안 흔들면 거의 평평하고 최대 주파수 없음 */
    private ObjectNode gait(int i, long start, double freq, boolean samePhase) {
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
        double[] v = new double[40];
        if (freq > 0) {
            double w = 2 * Math.PI * freq;
            // 앞뒤 1초를 더 만들어 대역 통과의 가장자리 효과를 줄인다(워치는 연속 신호를 거른다)
            double[] mag = new double[60];
            for (int k = 0; k < 60; k++) {
                double sec = (start - t0) / 1000.0 + (k - 10) / 10.0;
                double s = Math.sin(w * sec + phase(i, samePhase));
                double ax = -w * w * AMP[i][0] * s;
                double ay = -w * w * AMP[i][1] * s;
                double az = -w * w * AMP[i][2] * s;
                mag[k] = Math.sqrt(ax * ax + ay * ay + (az + MotionMatch.GRAVITY) * (az + MotionMatch.GRAVITY));
            }
            double[] band60 = MotionMatch.bandpass(mag);
            System.arraycopy(band60, 10, v, 0, 40);
            // 워치 앱은 서 있어도 대역 봉우리(0.5 이상)를 걸음으로 센다: 흔들기도 걸음 시점이 실린다
            for (long s = start + 200; s < start + 4_000; s += Math.round(1000 / freq)) {
                steps.add(s);
            }
        } else {
            for (int k = 0; k < 40; k++) {
                v[k] = 0.01 * Math.sin(k);
            }
        }
        double sumSq = 0;
        for (double x : v) {
            values.add(Math.round(x * 1000) / 1000.0);
            sumSq += x * x;
        }
        band.put("rms", Math.round(Math.sqrt(sumSq / 40) * 1000) / 1000.0);
        if (freq > 0) {
            band.put("peak_freq_hz", Math.min(3.0, freq));
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
        final BlockingQueue<JsonNode> received = new LinkedBlockingQueue<>();
        final ObjectMapper mapper;
        WebSocketSession session;
        final TextWebSocketHandler handler = new TextWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                JsonNode m = mapper.readTree(message.getPayload());
                // 배정 알림(S15P31A101-510)은 이 시험의 대상이 아니다
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
