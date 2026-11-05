package com.ssafy.simai.domain.watch;

import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import com.ssafy.simai.domain.watch.WatchBindings.Binding;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.DisposableBean;
import org.springframework.context.event.EventListener;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 워치 걸음 특징과 World 트랙 움직임을 대조해 워치와 익명 World ID 를 1:1 로 잇는다(S15P31A101-340, 확장 C.6, FR-43·44,
 * 설계 docs/design/server-watch-match.md).
 *
 * <ol>
 *   <li>world_state(10 Hz)마다 트랙별 손목(COCO17 9·10)·골반 높이와 수평 속력을 20초 쌓는다.</li>
 *   <li>gait_features 한 창(4초)이 오면 watch_feature 에 남기고, 트랙 표본이 그 창을 덮으면 트랙마다 점수를 낸다:
 *       워치 0.5~3 Hz 크기 신호와 트랙 연직 가속도(손목 둘·골반)의 밀어 본 상관(강화층), 손목·골반이 없으면 걷기/멈춤 일치(기본층).</li>
 *   <li>점수는 창마다 감쇠해 누적한다(몇 초 창 점수표). 헝가리안으로 1:1 을 고르고 점수·차이 문턱을 넘은 짝만 잇는다.</li>
 *   <li>ID 병합·분할이 보이면 그 World ID 의 점수와 대조 배정을 지우고 잠시(3초) 잇지 않는다(UNBOUND).</li>
 *   <li>World ID 별 채널 상태(BOUND·UNBOUND·LOST)가 바뀌면 channel_state 에 한 행.</li>
 * </ol>
 * 관리자 수동 배정(MANUAL)이 걸린 워치·World ID 는 대조가 건드리지 않는다. 모든 상태는 한 스레드(watch-match)에서만 바꾼다.
 */
@Service
public class WatchMotionMatcher implements DisposableBean {

    /** 창 점수 누적 감쇠. 1/(1−0.7) ≈ 3.3 창(약 7초)을 기억한다 */
    static final double DECAY = 0.7;
    static final double MAX_SCORE = 1 / (1 - DECAY);
    /** 이 누적 점수 이상이고 경쟁 짝보다 {@link #MARGIN} 이상 앞서야 잇는다(상관 0.7 이 세 창 이어지면 약 1.5) */
    static final double BIND_SCORE = 1.2;
    static final double MARGIN = 0.4;
    /** 이미 이은 짝은 이 아래로 떨어질 때까지 둔다(문턱 흔들림에 배정이 깜빡이지 않게) */
    static final double KEEP_SCORE = 0.6;
    /** 워치 창이 걷는 중인 기준(-339 실기기: 걷는 창 RMS ≥ 0.8 이고 최대 주파수가 있다) */
    static final double WALK_RMS = 0.8;
    /** 트랙이 걷는 중인 수평 속력(m/s) */
    static final double WALK_SPEED = 0.3;
    static final long SUSPECT_MS = 3_000;
    static final long TRACK_STALE_MS = 3_000;
    static final long TRACK_GONE_MS = 10_000;
    static final long BUFFER_MS = 20_000;
    /** 창 끝을 트랙 표본이 덮기를 기다리는 최대 시간. 넘으면 있는 표본으로 낸다 */
    static final long WINDOW_WAIT_MS = 5_000;
    /** 시계 차이·관측 지연을 흡수하려 밀어 보는 범위(±0.5초) */
    static final int MAX_LAG = 5;
    /** 벽시계보다 이만큼 넘게 앞선 world_state 는 버린다(잘못된 시각 하나가 '지금'을 미래에 묶지 않게) */
    static final long MAX_FUTURE_MS = 600_000;
    /** 받아들이는 창 길이 상한(계약은 4초, 스키마는 길이를 막지 않는다) */
    static final long MAX_WINDOW_MS = 10_000;
    /** 대조 스레드에 쌓아 둘 world_state 상한(10 Hz 로 5초) */
    static final int MAX_PENDING_WORLD = 50;
    /** 트랙 표본을 비교 시각에 맞출 때 허용하는 차이 */
    static final long SAMPLE_TOLERANCE_MS = 60;
    private static final Set<String> WRIST_LAYOUTS = Set.of("COCO17", "HALPE26");
    private static final int L_WRIST = 9;
    private static final int R_WRIST = 10;
    private static final Logger log = LoggerFactory.getLogger(WatchMotionMatcher.class);

    private record Sample(double leftZ, double rightZ, double pelvisZ, double speed) {
    }

    private static final class Track {
        final TreeMap<Long, Sample> samples = new TreeMap<>();
        long lastSeen;
        int revision = -1;
        long suspectUntil;
    }

    private static final class Window {
        final long start;
        final long end;
        final double[] values;
        final double rate;
        final boolean walking;
        final boolean still;
        boolean scored;

        Window(long start, long end, double[] values, double rate, double rms, boolean hasPeak) {
            this.start = start;
            this.end = end;
            this.values = values;
            this.rate = rate;
            this.walking = hasPeak && rms >= WALK_RMS;
            this.still = !hasPeak;
        }
    }

    private final WatchHub hub;
    private final WatchBindings bindings;
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final ScheduledExecutorService executor;

    private final Map<String, Track> tracks = new HashMap<>();
    private final Map<String, Deque<Window>> windows = new HashMap<>();
    private final Map<String, Map<String, Double>> scores = new HashMap<>();
    /** 마지막으로 channel_state 에 쓴 상태(World ID → "STATE|device") */
    private final Map<String, String> written = new HashMap<>();
    private final java.util.concurrent.atomic.AtomicInteger pendingWorld = new java.util.concurrent.atomic.AtomicInteger();
    private long latestWorldMs;

    WatchMotionMatcher(WatchHub hub, WatchBindings bindings, NamedParameterJdbcTemplate jdbc, ObjectMapper mapper,
            WatchProperties properties, Clock clock) {
        this.hub = hub;
        this.bindings = bindings;
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.clock = clock;
        this.executor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "watch-match");
            t.setDaemon(true);
            return t;
        });
        long ms = properties.matchInterval().toMillis();
        executor.scheduleWithFixedDelay(this::matchSafely, ms, ms, TimeUnit.MILLISECONDS);
    }

    @EventListener
    void onWorldState(WorldStateStored stored) {
        // 대조 스레드가 밀리면(DB 가 느림) 오래된 world_state 는 버린다. 쌓아 두면 payload 가 끝없이 붙들린다
        if (pendingWorld.get() >= MAX_PENDING_WORLD) {
            return;
        }
        pendingWorld.incrementAndGet();
        submit(() -> {
            try {
                ingestWorld(stored.envelope().payload());
            } finally {
                pendingWorld.decrementAndGet();
            }
        });
    }

    @EventListener
    void onGaitFeatures(GaitFeaturesReceived received) {
        submit(() -> ingestGait(received));
    }

    /** 시험용: 앞서 넘긴 world_state·gait_features 를 다 처리한 뒤 한 번 대조한다 */
    void matchNow() throws InterruptedException, ExecutionException {
        executor.submit(this::match).get();
    }

    private void submit(Runnable task) {
        try {
            executor.execute(() -> {
                try {
                    task.run();
                } catch (Throwable e) {
                    // execute 로 낸 작업의 예외는 어디에도 남지 않는다. 입력이 왜 안 들어오는지 알 수 있게 남긴다
                    log.warn("움직임 대조 입력 처리 실패: {}", e.getClass().getSimpleName(), e);
                }
            });
        } catch (RejectedExecutionException e) {
            // 서버가 내려가는 중이다
        }
    }

    /** 주기 작업은 예외가 밖으로 나가면 다음 실행이 취소된다. 무엇이든 잡아 남기고 다음 주기에 다시 한다 */
    private void matchSafely() {
        try {
            match();
        } catch (Throwable e) {
            log.warn("움직임 대조 실패: {}", e.getClass().getSimpleName(), e);
        }
    }

    // ------------------------------------------------------------------ 입력

    private void ingestWorld(JsonNode payload) {
        long t = payload.path("t_latest_global").asLong(0);
        if (t <= 0 || t > clock.millis() + MAX_FUTURE_MS) {
            return;
        }
        // '지금'은 마지막으로 받은 World 시각이다. 최댓값으로 두면 한 번 앞서 간 시각에 묶인다
        latestWorldMs = t;
        for (JsonNode track : payload.path("tracks")) {
            String id = track.path("world_id").asString("");
            if (id.isEmpty() || "LOST".equals(track.path("track_lifecycle").asString(""))) {
                continue;
            }
            Track buf = tracks.computeIfAbsent(id, k -> new Track());
            int revision = track.path("identity_revision").asInt(0);
            if (buf.revision >= 0 && revision > buf.revision) {
                identityChanged(id);
            }
            buf.revision = revision;
            // 병합: 합쳐진 옛 ID 의 배정은 끝낸다. 분할: 원래 ID 와 새 ID 모두 잠시 잇지 않는다
            for (JsonNode alias : track.path("aliases")) {
                String old = alias.asString("");
                if (!old.isEmpty() && tracks.containsKey(old)) {
                    identityChanged(old);
                    tracks.remove(old);
                    identityChanged(id);
                }
            }
            // 분할은 보통 두 ID 모두 identity_revision 이 올라 위에서 처리된다. 이것은 split_from 을 단 새 ID 가
            // 처음부터 나타나는 경우의 보강이다
            String splitFrom = track.path("split_from").asString("");
            if (!splitFrom.isEmpty() && buf.samples.isEmpty()) {
                identityChanged(splitFrom);
                identityChanged(id);
            }
            JsonNode v = track.path("velocity");
            double speed = v.isArray() && v.size() >= 2 ? Math.hypot(v.get(0).asDouble(), v.get(1).asDouble()) : Double.NaN;
            JsonNode pelvis = track.path("pelvis_xyz");
            double pelvisZ = pelvis.isArray() && pelvis.size() >= 3 ? pelvis.get(2).asDouble() : Double.NaN;
            JsonNode skeleton = track.path("skeleton");
            boolean wrists = WRIST_LAYOUTS.contains(skeleton.path("joint_layout").asString(""));
            buf.samples.put(t, new Sample(wrists ? jointZ(skeleton, L_WRIST) : Double.NaN,
                    wrists ? jointZ(skeleton, R_WRIST) : Double.NaN, pelvisZ, speed));
            buf.lastSeen = t;
            buf.samples.headMap(t - BUFFER_MS).clear();
        }
    }

    /** 관측(삼각측량)한 VALID·UNCERTAIN 관절의 Z. HMR 추정(inferred)·없음은 NaN(꾸민 움직임과 대조하지 않는다) */
    private static double jointZ(JsonNode skeleton, int index) {
        JsonNode joint = skeleton.path("joints").path(index);
        String validity = skeleton.path("validity").path(index).asString("");
        String source = skeleton.path("joint_source").path(index).asString("");
        if (!joint.isArray() || joint.size() < 3 || "UNAVAILABLE".equals(validity) || validity.isEmpty()
                || !"observed".equals(source)) {
            return Double.NaN;
        }
        return joint.get(2).asDouble();
    }

    private void identityChanged(String worldId) {
        scores.values().forEach(row -> row.remove(worldId));
        Track buf = tracks.get(worldId);
        if (buf != null) {
            buf.suspectUntil = latestWorldMs + SUSPECT_MS;
        }
        bindings.unbindMotion(null, worldId, "IDENTITY_CHANGED");
    }

    private void ingestGait(GaitFeaturesReceived received) {
        JsonNode p = received.payload();
        long start = p.path("window_start_ms").asLong();
        long end = p.path("window_end_ms").asLong();
        if (end <= start || end - start > MAX_WINDOW_MS) {
            // 스키마 밖 규칙(-339): 창 끝 > 시작. 긴 창은 배열 할당이 커져 대조 스레드를 멈출 수 있다
            log.warn("gait_features 창이 이상해 버림: device={} length_ms={}", received.deviceId(), end - start);
            return;
        }
        JsonNode offset = p.path("clock_offset_ms");
        // 워치 시계 − EC2 = offset. 모르면 받은 시각을 창 끝으로 본다(대략, 밀어 보기 ±0.5초로 흡수)
        long ec2Start = offset.isNumber() ? start - offset.asLong() : received.receivedAt().toEpochMilli() - (end - start);
        long ec2End = ec2Start + (end - start);
        JsonNode band = p.path("band_magnitude");
        JsonNode values = band.path("values");
        double[] series = new double[values.size()];
        for (int i = 0; i < series.length; i++) {
            series[i] = values.get(i).asDouble();
        }
        int stored = jdbc.update("""
                INSERT INTO watch_feature (measured_at, device_id, features, received_at)
                VALUES (:measured, :device, CAST(:features AS jsonb), :received)
                ON CONFLICT DO NOTHING
                """, new MapSqlParameterSource("measured", utc(ec2Start)).addValue("device", received.deviceId())
                .addValue("features", mapper.writeValueAsString(p)).addValue("received", utc(received.receivedAt().toEpochMilli())));
        if (stored == 0) {
            return; // 같은 창을 다시 받았다(재전송). 점수에 두 번 넣지 않는다
        }
        Deque<Window> queue = windows.computeIfAbsent(received.deviceId(), k -> new ArrayDeque<>());
        queue.addLast(new Window(ec2Start, ec2End, series, band.path("rate_hz").asDouble(MotionMatch.RATE_HZ),
                band.path("rms").asDouble(0), band.path("peak_freq_hz").isNumber()));
        while (queue.size() > 16) {
            queue.removeFirst();
        }
    }

    // ------------------------------------------------------------------ 대조

    private void match() {
        long now = latestWorldMs;
        if (now == 0) {
            return;
        }
        scoreWindows(now);
        List<Binding> active = bindings.active();
        Set<String> manualDevices = new HashSet<>();
        Set<String> manualWorlds = new HashSet<>();
        Map<String, String> motion = new HashMap<>();
        for (Binding b : active) {
            if ("MANUAL".equals(b.method())) {
                manualDevices.add(b.deviceId());
                manualWorlds.add(b.worldId());
            } else {
                motion.put(b.deviceId(), b.worldId());
            }
        }
        List<String> rows = new ArrayList<>();
        for (String device : new LinkedHashSet<>(windows.keySet())) {
            if (!manualDevices.contains(device) && hub.isConnected(device)) {
                rows.add(device);
            }
        }
        List<String> cols = new ArrayList<>();
        tracks.forEach((id, t) -> {
            if (t.lastSeen >= now - TRACK_STALE_MS && t.suspectUntil <= now && !manualWorlds.contains(id)) {
                cols.add(id);
            }
        });
        Map<String, String> accepted = assign(rows, cols);

        // 이미 이은 대조 배정: 같은 짝이 다시 뽑혔거나, 경쟁 짝이 없고 점수가 KEEP 이상(또는 아직 근거가 없음)이면 둔다.
        // 근거 없음은 서버 재시작 직후 점수표가 빈 경우다. 그때 배정을 다 풀면 다시 쌓는 동안 모두 UNBOUND 가 된다
        Set<String> acceptedWorlds = new HashSet<>(accepted.values());
        for (Map.Entry<String, String> e : motion.entrySet()) {
            String device = e.getKey();
            String world = e.getValue();
            Track t = tracks.get(world);
            boolean same = world.equals(accepted.get(device));
            boolean contested = accepted.containsKey(device) || acceptedWorlds.contains(world);
            boolean alive = t != null && t.lastSeen >= now - TRACK_GONE_MS && t.suspectUntil <= now;
            Double evidence = scores.getOrDefault(device, Map.of()).get(world);
            if (same || (!contested && alive && (evidence == null || evidence >= KEEP_SCORE))) {
                accepted.remove(device);
                continue;
            }
            bindings.unbindMotion(device, world, "MATCH_LOST");
        }
        accepted.forEach((device, world) -> {
            if (bindings.bindByMotion(device, world, score(device, world) / MAX_SCORE)) {
                log.info("움직임 대조로 이음: device={} world_id={} score={}", device, world, String.format("%.2f", score(device, world)));
            }
        });
        writeChannelStates(now);
        tracks.values().removeIf(t -> t.lastSeen < now - BUFFER_MS);
        // 사라진 트랙의 점수 칸, 창이 없고 연결도 없는 워치를 지운다(익명 ID 는 계속 새로 나온다)
        scores.values().forEach(row -> row.keySet().retainAll(tracks.keySet()));
        windows.entrySet().removeIf(e -> e.getValue().isEmpty() && !hub.isConnected(e.getKey()));
        scores.keySet().removeIf(device -> !windows.containsKey(device));
    }

    private void scoreWindows(long now) {
        for (Map.Entry<String, Deque<Window>> e : windows.entrySet()) {
            Map<String, Double> row = scores.computeIfAbsent(e.getKey(), k -> new HashMap<>());
            for (Window w : e.getValue()) {
                // 트랙 표본이 창 끝을 덮을 때까지 기다리되, 벽시계로 창 끝 + 5초가 지나면 있는 표본으로 낸다
                if (w.scored || (w.end > now && clock.millis() < w.end + WINDOW_WAIT_MS)) {
                    continue;
                }
                w.scored = true;
                // 근거를 낸 칸만 감쇠·누적한다. 근거가 없는 창(서 있음·가림·손에 들고 만짐)은 반대 근거가 아니라
                // 점수를 그대로 둔다. 줄이면 멈춰 일하는 동안 배정이 풀려 그때 난 위험에 진동이 가지 않는다
                tracks.forEach((id, t) -> {
                    if (t.suspectUntil > w.end || t.lastSeen < w.start) {
                        return;
                    }
                    double s = windowScore(w, t);
                    if (!Double.isNaN(s)) {
                        row.merge(id, s, (old, add) -> old * DECAY + add);
                    }
                });
            }
            e.getValue().removeIf(w -> w.scored && w.end < now - BUFFER_MS);
        }
    }

    /**
     * 한 창의 워치 ↔ 트랙 점수(−1~1). 워치가 걷는 중이면 연직 가속도 상관(손목 둘, 골반은 0.8 배). 비교할 관절이 없으면
     * 걷기 일치 +0.2·불일치 −0.5. 워치가 멈춰 있으면 트랙이 걸을 때만 −0.5(서 있는 사람끼리는 구분할 근거가 없다).
     */
    private double windowScore(Window w, Track t) {
        int n = (int) Math.round((w.end - w.start) * MotionMatch.RATE_HZ / 1000);
        double[] watch = new double[n];
        double[] left = new double[n];
        double[] right = new double[n];
        double[] pelvis = new double[n];
        double[] speed = new double[n];
        for (int k = 0; k < n; k++) {
            int idx = (int) Math.round(k * w.rate / MotionMatch.RATE_HZ);
            watch[k] = idx < w.values.length ? w.values[idx] : Double.NaN;
            Sample s = nearest(t, w.start + Math.round(k * 1000 / MotionMatch.RATE_HZ));
            left[k] = s == null ? Double.NaN : s.leftZ();
            right[k] = s == null ? Double.NaN : s.rightZ();
            pelvis[k] = s == null ? Double.NaN : s.pelvisZ();
            speed[k] = s == null ? Double.NaN : s.speed();
        }
        Boolean trackWalking = MotionMatch.coverage(speed) >= 0.5 ? mean(speed) >= WALK_SPEED : null;
        if (!w.walking) {
            if (trackWalking == null || !w.still) {
                return Double.NaN; // 손에 들고 만지는 창 등: 근거로 쓰지 않는다
            }
            return trackWalking ? -0.5 : Double.NaN;
        }
        double corr = Double.NaN;
        for (double[] z : new double[][] {left, right}) {
            corr = max(corr, correlate(watch, z, 1.0));
        }
        corr = max(corr, correlate(watch, pelvis, 0.8));
        if (!Double.isNaN(corr)) {
            return corr;
        }
        if (trackWalking == null) {
            return Double.NaN;
        }
        return trackWalking ? 0.2 : -0.5;
    }

    private static double correlate(double[] watch, double[] z, double weight) {
        double[] accel = MotionMatch.bandpass(MotionMatch.secondDifference(z));
        if (MotionMatch.coverage(accel) < 0.6) {
            return Double.NaN;
        }
        double r = MotionMatch.bestLaggedCorrelation(watch, accel, MAX_LAG);
        return Double.isNaN(r) ? r : r * weight;
    }

    private static Sample nearest(Track t, long at) {
        Map.Entry<Long, Sample> lo = t.samples.floorEntry(at);
        Map.Entry<Long, Sample> hi = t.samples.ceilingEntry(at);
        Map.Entry<Long, Sample> best = lo == null ? hi : hi == null ? lo : (at - lo.getKey() <= hi.getKey() - at ? lo : hi);
        return best == null || Math.abs(best.getKey() - at) > SAMPLE_TOLERANCE_MS ? null : best.getValue();
    }

    /** 헝가리안 1:1 뒤 점수·차이 문턱을 넘은 짝만(워치 → World ID) */
    private Map<String, String> assign(List<String> rows, List<String> cols) {
        Map<String, String> out = new HashMap<>();
        if (rows.isEmpty() || cols.isEmpty()) {
            return out;
        }
        double[][] cost = new double[rows.size()][cols.size()];
        for (int i = 0; i < rows.size(); i++) {
            for (int j = 0; j < cols.size(); j++) {
                cost[i][j] = -score(rows.get(i), cols.get(j));
            }
        }
        int[] pick = MotionMatch.hungarian(cost);
        for (int i = 0; i < rows.size(); i++) {
            int j = pick[i];
            if (j < 0) {
                continue;
            }
            double s = -cost[i][j];
            double rival = Double.NEGATIVE_INFINITY;
            for (int k = 0; k < cols.size(); k++) {
                if (k != j) {
                    rival = Math.max(rival, -cost[i][k]);
                }
            }
            for (int k = 0; k < rows.size(); k++) {
                if (k != i) {
                    rival = Math.max(rival, -cost[k][j]);
                }
            }
            if (s >= BIND_SCORE && (rival == Double.NEGATIVE_INFINITY || s - rival >= MARGIN)) {
                out.put(rows.get(i), cols.get(j));
            }
        }
        return out;
    }

    private double score(String device, String world) {
        return scores.getOrDefault(device, Map.of()).getOrDefault(world, 0.0);
    }

    /** 보이는 World ID 마다 BOUND(배정·연결)·LOST(배정·끊김)·UNBOUND(그 밖). 바뀐 것만 쓴다 */
    private void writeChannelStates(long now) {
        Map<String, Binding> byWorld = new HashMap<>();
        bindings.active().forEach(b -> byWorld.put(b.worldId(), b));
        Set<String> visible = new HashSet<>();
        tracks.forEach((id, t) -> {
            if (t.lastSeen >= now - TRACK_GONE_MS) {
                visible.add(id);
            }
        });
        for (String id : visible) {
            Binding b = byWorld.get(id);
            String state = b == null ? "UNBOUND" : hub.isConnected(b.deviceId()) ? "BOUND" : "LOST";
            writeState(now, id, state, b);
        }
        // 사라진(10초 넘게 안 보이거나 병합된) ID 는 UNBOUND 로 마감한다. 마지막 행이 BOUND 로 남으면 화면이 믿는다
        for (String id : new ArrayList<>(written.keySet())) {
            if (!visible.contains(id)) {
                if (!written.get(id).startsWith("UNBOUND|")) {
                    writeState(now, id, "UNBOUND", null);
                }
                written.remove(id);
            }
        }
    }

    /** 바뀐 때만 한 행. 같은 World 시각에 이미 행이 있으면(시각이 멈춤) 쓰지 못했으니 기억하지 않고 다음에 다시 쓴다 */
    private void writeState(long now, String id, String state, Binding b) {
        String key = state + "|" + (b == null ? "" : b.deviceId());
        if (key.equals(written.get(id))) {
            return;
        }
        int rows = jdbc.update("""
                INSERT INTO channel_state (observed_at, world_id, state, device_id, confidence)
                VALUES (:at, :world, :state, :device, :confidence)
                ON CONFLICT DO NOTHING
                """, new MapSqlParameterSource("at", utc(now)).addValue("world", id).addValue("state", state)
                .addValue("device", b == null ? null : b.deviceId())
                .addValue("confidence", b == null ? null : (Object) b.score()));
        if (rows > 0) {
            written.put(id, key);
        }
    }

    private static double mean(double[] x) {
        double sum = 0;
        int n = 0;
        for (double v : x) {
            if (!Double.isNaN(v)) {
                sum += v;
                n++;
            }
        }
        return n == 0 ? Double.NaN : sum / n;
    }

    private static double max(double a, double b) {
        return Double.isNaN(a) ? b : Double.isNaN(b) ? a : Math.max(a, b);
    }

    private static OffsetDateTime utc(long epochMs) {
        return OffsetDateTime.ofInstant(Instant.ofEpochMilli(epochMs), ZoneOffset.UTC);
    }

    @Override
    public void destroy() {
        executor.shutdownNow();
    }
}
