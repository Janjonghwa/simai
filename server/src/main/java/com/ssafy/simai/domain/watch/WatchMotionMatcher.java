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
 *   <li>자리 이어받기(S15P31A101-793): 배정된 트랙이 사라진 뒤 5초 안에 그 마지막 자리 1.5 m 안에 새 트랙 하나만 나타나면 배정을
 *       새 ID 로 옮긴다(임시). 같은 짝을 대조가 다시 뽑으면 확정, 반대 근거가 쌓이면 푼다.</li>
 *   <li>World ID 별 채널 상태(BOUND·UNBOUND·LOST)가 바뀌면 channel_state 에 한 행.</li>
 * </ol>
 * 배정은 시간이 지나도 풀지 않으며(Re-ID 로 원래 ID 가 다시 보이면 그대로 유지), 관리자 수동 배정(MANUAL, 확정된 것)이 걸린 워치·World ID 는
 * 그 World ID 가 보이는 동안 대조가 건드리지 않는다. 배정된 World ID 가 지금 보이지 않을 때(>10초 미관측, 수동 확정 포함)만 새 후보(걸음 대조 또는
 * 시연 스위치 1명 자동 배정)가 옛 행을 REPLACED 로 닫고 갈아 끼울 수 있다(S15P31A101-826). 모든 상태는 한 스레드(watch-match)에서만 바꾼다.
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
    /**
     * 손 흔들기 체크인(S15P31A101-508). 워치 창 RMS 가 이 이상이면 크게 흔드는 창으로 본다(걷는 창 0.8 보다 훨씬 큼: 손목을
     * 0.1 m 이상 2 Hz 안팎으로 흔들면 |g + a| 가 수 m/s² 로 변한다). 워치 앱은 서 있어도 걸음 시점을 세므로(대역 봉우리 0.5 이상)
     * 걸음 시점 유무로는 흔들기와 걷기를 가를 수 없다. 가르는 것은 같은 창에서 몸통이 멈춰 있는 트랙인가이다
     */
    static final double WAVE_RMS = 2.0;
    /** 몸통이 멈춰 있다고 보는 기준: 평균 수평 속력(m/s)과 골반 높이 표준편차(m, 삼각측량 잡음 1~2 cm 보다 큼) */
    static final double STILL_SPEED = 0.2;
    static final double STILL_PELVIS_STD = 0.03;
    /** 손목이 크게 움직인다고 보는 기준: 대역 통과한 3D 위치의 RMS(m). 잡음 1~2 cm 보다 크고 손 흔들기(진폭 10 cm 이상, RMS 7 cm)보다 작게 */
    static final double WAVE_MOVE_M = 0.04;
    /** 몸통이 멈춰 있고 손목도 관측되는데 손목이 움직이지 않으면, 크게 흔드는 워치의 주인이 아니라는 반대 근거 */
    static final double WAVE_REJECT = -0.3;
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
    static final int MAX_MERGE_HANDLED = 2_000;
    /** 트랙 표본을 비교 시각에 맞출 때 허용하는 차이 */
    static final long SAMPLE_TOLERANCE_MS = 60;
    /**
     * 자리 이어받기(S15P31A101-793). 배정된 트랙이 마지막으로 보인 뒤 이 시간 안에(가려졌다 나오는 데 걸리는 시간, Tier 0 가 끊긴 트랙을
     * 새 ID 로 다시 내는 데 1~3초) 마지막 자리에서 이 거리 안에(한 사람이 2 m/s 로 반 초 남짓 움직이는 거리, 나란히 선 두 사람의 간격
     * 0.5~1 m 보다 넓어 후보가 둘이면 옮기지 않는 쪽으로 걸린다) 새 트랙이 나타나야 한다
     */
    static final long HANDOFF_WINDOW_MS = 5_000;
    static final double HANDOFF_RADIUS_M = 1.5;
    /** 새 트랙이 옛 트랙과 이보다 오래 같이 보였으면 다른 사람이다(Tier 0 가 한 사람을 잠깐 둘로 보는 겹침만 허용) */
    static final long HANDOFF_OVERLAP_MS = 1_000;
    /** 새 트랙이 이만큼 보인 뒤에 옮긴다. 바로 옮기면 반 박자 늦게 나타난 두 번째 후보(같은 사람의 두 조각·옆 사람)를 못 본다 */
    static final long HANDOFF_SETTLE_MS = 300;
    /** 시연 스위치 1명 자동 배정: LIVE 추적 사람이 정확히 1명이고 이 시간 넘게 이어졌을 때 잇는다(S15P31A101-826) */
    static final long DEMO_SINGLE_MIN_MS = 5_000;
    private static final Set<String> WRIST_LAYOUTS = Set.of("COCO17", "HALPE26");
    private static final int L_WRIST = 9;
    private static final int R_WRIST = 10;
    private static final Logger log = LoggerFactory.getLogger(WatchMotionMatcher.class);

    /** 손목은 3D(x, y, z), 관측 없음·추정(inferred)은 NaN. x·y 는 구역 판정용 수평 위치(position_xy, 자리 이어받기에 쓴다) */
    private record Sample(double[] left, double[] right, double pelvisZ, double speed, double x, double y) {
        double leftZ() {
            return left[2];
        }

        double rightZ() {
            return right[2];
        }
    }

    private static final double[] NO_JOINT = {Double.NaN, Double.NaN, Double.NaN};

    private static final class Track {
        final TreeMap<Long, Sample> samples = new TreeMap<>();
        long lastSeen;
        /** 처음 보인 World 시각과 그때 자리(자리 이어받기의 "새 트랙"·"그 자리" 판단) */
        long firstSeen = -1;
        double firstX = Double.NaN;
        double firstY = Double.NaN;
        /** 마지막으로 보인 자리 */
        double lastX = Double.NaN;
        double lastY = Double.NaN;
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
        final double rms;
        boolean scored;

        Window(long start, long end, double[] values, double rate, double rms, boolean hasPeak) {
            this.start = start;
            this.end = end;
            this.values = values;
            this.rate = rate;
            this.rms = rms;
            this.walking = hasPeak && rms >= WALK_RMS;
            this.still = !hasPeak;
        }
    }

    private final WatchHub hub;
    private final WatchBindings bindings;
    private final PersonDisplayNames displayNames;
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final WatchProperties properties;
    private final Clock clock;
    private final ScheduledExecutorService executor;
    /** 시연 스위치(simai.watch.demo-device, S15P31A101-826). 시험이 바꿀 수 있게 final 이 아니다 */
    private volatile String demoDevice;
    /**
     * 마지막 대조 때의 지금 배정(World ID → 배정). 자리 이어받기는 10 Hz 입력마다 보므로 DB 대신 이것을 보고, 옮기면 바로 고친다.
     * 대조 주기(2초) 사이에 들어온 수동 배정은 다음 주기까지 모른다(그 사이 그 트랙이 사라지면 이어받지 못하고 관리자가 다시 넣는다)
     */
    private Map<String, Binding> boundByWorld = new HashMap<>();

    /** 이미 처리한 병합("옛>새"). aliases 는 한동안 계속 실려 오므로 한 번만 처리하려고 기억한다(오래된 것부터 버림) */
    private final LinkedHashSet<String> mergeHandled = new LinkedHashSet<>();
    private final Map<String, Track> tracks = new HashMap<>();
    private final Map<String, Deque<Window>> windows = new HashMap<>();
    private final Map<String, Map<String, Double>> scores = new HashMap<>();
    /** 마지막으로 channel_state 에 쓴 상태(World ID → "STATE|device") */
    private final Map<String, String> written = new HashMap<>();
    private final java.util.concurrent.atomic.AtomicInteger pendingWorld = new java.util.concurrent.atomic.AtomicInteger();
    private long latestWorldMs;

    WatchMotionMatcher(WatchHub hub, WatchBindings bindings, PersonDisplayNames displayNames, NamedParameterJdbcTemplate jdbc,
            ObjectMapper mapper, WatchProperties properties, Clock clock) {
        this.hub = hub;
        this.bindings = bindings;
        this.displayNames = displayNames;
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.properties = properties;
        this.clock = clock;
        this.demoDevice = properties.demoDevice();
        this.executor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "watch-match");
            t.setDaemon(true);
            return t;
        });
        long ms = properties.matchInterval().toMillis();
        executor.scheduleWithFixedDelay(this::matchSafely, ms, ms, TimeUnit.MILLISECONDS);
    }

    /** 시험용: 시연 스위치를 바꾼다(null 이면 끔) */
    void setDemoDevice(String deviceId) {
        this.demoDevice = deviceId == null || deviceId.isBlank() ? null : deviceId.strip();
    }

    /** 시험용: 대조기 메모리 상태를 비운다(앞 시험의 트랙·시각이 다음 시험의 1명 판정이나 시각 한도에 섞이지 않게) */
    void resetForTest() throws InterruptedException, ExecutionException {
        executor.submit(() -> {
            tracks.clear();
            windows.clear();
            scores.clear();
            written.clear();
            boundByWorld.clear();
            mergeHandled.clear();
            latestWorldMs = 0;
            demoDevice = properties.demoDevice();
        }).get();
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

    /**
     * 시험용: 앞서 넘긴 world_state·gait_features 를 다 처리할 때까지 기다린다(대조는 하지 않는다). 시험이 몇 초 분량의
     * world_state 를 한꺼번에 넣을 때 {@link #MAX_PENDING_WORLD} 를 넘겨 버려지지 않게 중간중간 부른다(S15P31A101-674)
     */
    void awaitInputs() throws InterruptedException, ExecutionException {
        executor.submit(() -> { }).get();
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
            // 병합(S15P31A101-508): 옛 ID 에 걸린 배정(MOTION_MATCH·MANUAL)은 살아남은 ID 로 옮긴다. 같은 사람의 트랙 조각이 합쳐진 것이고
            // 12차에서 ID 가 자주 바뀌어 매번 풀리면 워치가 쓸모없어서다. 옮긴 배정은 의심 구간 없이 이어 가고 옛 점수를 같이 옮긴다.
            // 옮기지 못하면(둘 다 대조 배정·수동 충돌) 예전처럼 둘 다 풀고 3초 잇지 않는다
            for (JsonNode alias : track.path("aliases")) {
                String old = alias.asString("");
                if (old.isEmpty() || old.equals(id) || !mergeHandled.add(old + ">" + id)) {
                    continue; // 같은 병합은 aliases 에 계속 실려도 한 번만 처리한다
                }
                while (mergeHandled.size() > MAX_MERGE_HANDLED) {
                    mergeHandled.remove(mergeHandled.iterator().next());
                }
                Binding moved = bindings.followMerge(old, id);
                if (moved != null) {
                    carryScores(old, id, moved.deviceId());
                    tracks.remove(old);
                } else if (tracks.containsKey(old)) {
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
            JsonNode xy = track.path("position_xy");
            // null·숫자 아님은 asDouble 이 0 을 주므로(원점에 있는 것으로 보게 된다) 숫자일 때만 자리로 쓴다
            boolean hasXy = xy.isArray() && xy.size() >= 2 && xy.get(0).isNumber() && xy.get(1).isNumber();
            double x = hasXy ? xy.get(0).asDouble() : Double.NaN;
            double y = hasXy ? xy.get(1).asDouble() : Double.NaN;
            JsonNode skeleton = track.path("skeleton");
            boolean wrists = WRIST_LAYOUTS.contains(skeleton.path("joint_layout").asString(""));
            buf.samples.put(t, new Sample(wrists ? joint(skeleton, L_WRIST) : NO_JOINT,
                    wrists ? joint(skeleton, R_WRIST) : NO_JOINT, pelvisZ, speed, x, y));
            if (buf.firstSeen < 0) {
                buf.firstSeen = t;
            }
            if (hasXy) {
                if (Double.isNaN(buf.firstX)) {
                    buf.firstX = x; // 처음 자리를 안 프레임(첫 프레임에 자리가 없을 수 있다)
                    buf.firstY = y;
                }
                buf.lastX = x;
                buf.lastY = y;
            }
            buf.lastSeen = t;
            buf.samples.headMap(t - BUFFER_MS).clear();
        }
        handoffVanished(t);
    }

    /**
     * 자리 이어받기(S15P31A101-793). 이번 프레임에 없는 배정된 트랙(5초 안에 보였던 것)마다, 그 마지막 자리 1.5 m 안에 처음 나타난
     * 배정 없는 트랙이 꼭 하나이고(옛 트랙과 1초 넘게 같이 보인 것은 다른 사람), 옛 자리 근처에 배정 없는 다른 트랙이 없고, 그 새 트랙 근처에
     * 사라진 배정 트랙이 둘이 아니면(두 사람이 같이 가려짐) 배정을 새 ID 로 옮긴다. 후보가 둘이면 옮기지 않는다: 바꿔 끼우는 것이
     * 안 붙는 것보다 나쁘다(틀린 사람 손목이 울린다). 옮긴 배정은 임시이고 옛 점수는 버린다. 미확인 번호·이름은 병합처럼 따라간다.
     * Tier 0 는 가림이든 퇴장이든 측정 공백 3초 뒤 LOST 한 번을 내고 트랙을 뺀다(tier0-world-track.md)라 둘을 가를 수 없다. 퇴장한
     * 사람 자리로 5초 안에 다른 사람이 들어오면 틀리게 옮길 수 있고, 그때는 임시 배정의 반증(걸음 불일치)·관리자 재배정이 막는다.
     */
    private void handoffVanished(long now) {
        if (boundByWorld.isEmpty()) {
            return;
        }
        List<Track> vanished = new ArrayList<>();
        List<String> vanishedIds = new ArrayList<>();
        boundByWorld.forEach((world, b) -> {
            Track old = tracks.get(world);
            if (old != null && old.lastSeen < now && old.lastSeen >= now - HANDOFF_WINDOW_MS && old.suspectUntil <= old.lastSeen
                    && !Double.isNaN(old.lastX)) {
                vanished.add(old);
                vanishedIds.add(world);
            }
        });
        for (int i = 0; i < vanished.size(); i++) {
            Track old = vanished.get(i);
            String oldId = vanishedIds.get(i);
            String candidate = null;
            boolean ambiguous = false;
            for (Map.Entry<String, Track> e : tracks.entrySet()) {
                Track t = e.getValue();
                if (t.lastSeen < now || boundByWorld.containsKey(e.getKey()) || Double.isNaN(t.firstX)) {
                    continue; // 지금 안 보이거나, 이미 배정이 있거나, 자리를 모른다
                }
                boolean nearNow = Math.hypot(t.lastX - old.lastX, t.lastY - old.lastY) <= HANDOFF_RADIUS_M;
                boolean young = t.firstSeen >= old.lastSeen - HANDOFF_OVERLAP_MS && t.suspectUntil <= now
                        && Math.hypot(t.firstX - old.lastX, t.firstY - old.lastY) <= HANDOFF_RADIUS_M;
                if (young) {
                    ambiguous |= candidate != null;
                    candidate = e.getKey();
                } else if (nearNow) {
                    ambiguous = true; // 옛 자리에 배정 없는 다른 사람이 서 있다
                }
            }
            if (candidate == null || ambiguous) {
                continue;
            }
            Track fresh = tracks.get(candidate);
            if (fresh.firstSeen > now - HANDOFF_SETTLE_MS) {
                continue; // 조금 더 보고 옮긴다(두 번째 후보가 뒤따라 나오면 위에서 모호해진다)
            }
            boolean othersVanishedNear = false;
            for (int k = 0; k < vanished.size(); k++) {
                Track other = vanished.get(k);
                if (k != i && Math.hypot(other.lastX - fresh.firstX, other.lastY - fresh.firstY) <= HANDOFF_RADIUS_M) {
                    othersVanishedNear = true; // 두 사람이 같이 가려졌다 하나만 나왔다: 누구인지 모른다
                }
            }
            if (othersVanishedNear) {
                continue;
            }
            Binding moved = bindings.handoff(oldId, candidate);
            boundByWorld.remove(oldId);
            tracks.remove(oldId);
            if (moved == null) {
                continue; // 그 사이 배정이 바뀌었다(수동 배정·해제). 다음 대조가 DB 를 다시 읽는다
            }
            boundByWorld.put(candidate, moved);
            // 옛 점수는 옮기지 않는다: 옮기면 새 트랙의 걸음을 한 창도 보지 않고 옛 점수만으로 확정될 수 있다. 확정은 새 트랙의 근거로만
            scores.values().forEach(row -> row.remove(oldId));
            log.info("자리 이어받기: device={} world_id {} → {} (방식 {}, 임시)", moved.deviceId(), oldId, candidate, moved.method());
            displayNames.recordHandoff(oldId, candidate);
        }
    }

    /** 관측(삼각측량)한 VALID·UNCERTAIN 관절의 XYZ. HMR 추정(inferred)·없음은 NaN(꾸민 움직임과 대조하지 않는다) */
    private static double[] joint(JsonNode skeleton, int index) {
        JsonNode joint = skeleton.path("joints").path(index);
        String validity = skeleton.path("validity").path(index).asString("");
        String source = skeleton.path("joint_source").path(index).asString("");
        if (!joint.isArray() || joint.size() < 3 || "UNAVAILABLE".equals(validity) || validity.isEmpty()
                || !"observed".equals(source)) {
            return NO_JOINT;
        }
        return new double[] {joint.get(0).asDouble(), joint.get(1).asDouble(), joint.get(2).asDouble()};
    }

    /** 병합으로 옮긴 배정의 점수만 살아남은 ID 로 옮기고, 그 밖의 칸은 지운다(합쳐진 트랙의 움직임이 달라졌다) */
    private void carryScores(String old, String survivor, String device) {
        scores.forEach((d, row) -> {
            Double carried = row.remove(old);
            row.remove(survivor);
            if (d.equals(device) && carried != null) {
                row.put(survivor, carried);
            }
        });
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
        Map<String, Binding> byWorld = new HashMap<>();
        active.forEach(b -> byWorld.put(b.worldId(), b));
        boundByWorld = byWorld;
        Set<String> manualDevices = new HashSet<>();
        Set<String> manualWorlds = new HashSet<>();
        // 배정된 World ID 가 지금 보이지 않는(>10초 미관측) 확정 수동 배정은 새 걸음 대조 후보가 REPLACED 로 갈아 끼울 수 있다(S15P31A101-826)
        Map<String, Long> replaceableManual = new HashMap<>();
        // 대조가 다루는 배정: 대조 배정·시연 1명 배정·임시 이어받기. 확정 수동은 그 World ID 가 보이는 동안 행·열에서 뺀다
        Map<String, Binding> motion = new HashMap<>();
        for (Binding b : active) {
            if (b.manualFixed()) {
                if (isTrackVisible(b.worldId(), now)) {
                    manualDevices.add(b.deviceId());
                    manualWorlds.add(b.worldId());
                } else {
                    replaceableManual.put(b.deviceId(), b.bindingId());
                }
            } else {
                motion.put(b.deviceId(), b);
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

        // 배정은 시간이 지나도 풀지 않는다(Re-ID 로 원래 ID 가 다시 보이면 그대로 유지, S15P31A101-826).
        // 경쟁 짝이 뽑혔으면 아래 bindByMotion 이 옛 행을 REPLACED 로 닫고 새 행을 넣는다.
        // 경쟁이 없을 때는 ID 교정 직후이거나 점수가 유지 문턱 아래로 떨어진 경우에만 MATCH_LOST 로 푼다.
        Set<String> acceptedWorlds = new HashSet<>(accepted.values());
        for (Map.Entry<String, Binding> e : motion.entrySet()) {
            String device = e.getKey();
            Binding b = e.getValue();
            String world = b.worldId();
            Track t = tracks.get(world);
            boolean same = world.equals(accepted.get(device));
            boolean contested = accepted.containsKey(device) || acceptedWorlds.contains(world);
            boolean suspect = t != null && t.suspectUntil > now;
            Double evidence = scores.getOrDefault(device, Map.of()).get(world);
            if (same && b.demoSingle()) {
                // 시연 1명 자동 배정을 걸음 대조가 같은 짝으로 뽑았다: accepted 에 남겨 아래 bindByMotion 으로 MOTION_MATCH 승격
                continue;
            }
            if (same && b.tentative()) {
                // 자리로 옮긴 배정을 대조가 같은 짝으로 다시 뽑았다: 확정(수동이면 다시 관리자 것). 새 행을 만들지 않는다
                if (bindings.confirmHandoff(device, world)) {
                    log.info("자리 이어받기 확정: device={} world_id={} score={}", device, world, String.format("%.2f", score(device, world)));
                }
                accepted.remove(device);
                continue;
            }
            // 임시 이어받기·시연 1명 배정은 반대 근거(음수)가 쌓일 때만 푼다. 걷기 일치 같은 약한 근거(0.2)로 풀지 않으려고 문턱을 0 으로 둔다
            double keep = b.provisional() ? 0 : KEEP_SCORE;
            if (same || (!contested && !suspect && (evidence == null || evidence >= keep))) {
                accepted.remove(device);
                continue;
            }
            if (contested) {
                continue;
            }
            bindings.unbindMotion(device, world, "MATCH_LOST");
        }
        accepted.forEach((device, world) -> {
            Long replaceId = replaceableManual.get(device);
            if (bindings.bindByMotion(device, world, score(device, world) / MAX_SCORE, replaceId)) {
                log.info("움직임 대조로 이음: device={} world_id={} score={}{}", device, world,
                        String.format("%.2f", score(device, world)), replaceId == null ? "" : " (사라진 수동 배정 교체)");
            }
        });
        bindDemoSingleIfEligible(now);
        writeChannelStates(now);
        // 이번 대조가 잇고 푼 결과까지 반영한 배정 목록을 자리 이어받기가 쓴다(다음 대조까지 2초 동안 옛 목록으로 보지 않게)
        Map<String, Binding> latest = new HashMap<>();
        bindings.active().forEach(b -> latest.put(b.worldId(), b));
        boundByWorld = latest;
        tracks.values().removeIf(t -> t.lastSeen < now - BUFFER_MS);
        // 사라진 트랙의 점수 칸, 창이 없고 연결도 없는 워치를 지운다(익명 ID 는 계속 새로 나온다)
        scores.values().forEach(row -> row.keySet().retainAll(tracks.keySet()));
        windows.entrySet().removeIf(e -> e.getValue().isEmpty() && !hub.isConnected(e.getKey()));
        scores.keySet().removeIf(device -> !windows.containsKey(device));
    }

    /** 배정된 World ID 가 최근 {@link #TRACK_GONE_MS}(10초) 안에 관측돼 지금 보이는지 판정한다(S15P31A101-826) */
    private boolean isTrackVisible(String worldId, long now) {
        Track t = tracks.get(worldId);
        return t != null && t.lastSeen >= now - TRACK_GONE_MS;
    }

    /**
     * 시연 스위치 1명 자동 배정(S15P31A101-826): simai.watch.demo-device 가 켜져 있고, LIVE 추적 사람이 정확히 1명이며
     * 그 트랙이 5초 넘게 이어졌을 때, 시연 워치에 배정이 없거나 기존 배정의 World ID 가 지금 보이지 않으면(>10초 미관측, 수동 확정 포함)
     * 시연 워치를 그 World ID 에 잇는다(DEMO_SINGLE, 임시 성격, 기존 배정은 REPLACED 로 닫음).
     * 사람이 2명 이상이거나, 걸음 대조가 이미 반대 근거(음수)를 냈거나, ID 교정 직후면 잇지 않는다.
     */
    private boolean bindDemoSingleIfEligible(long now) {
        String demo = demoDevice;
        if (demo == null) {
            return false;
        }
        String soleWorld = null;
        Track soleTrack = null;
        for (Map.Entry<String, Track> e : tracks.entrySet()) {
            Track t = e.getValue();
            if (t.lastSeen >= now - TRACK_STALE_MS) {
                if (soleWorld != null) {
                    return false; // 2명 이상이면 걸음 대조에 맡긴다
                }
                soleWorld = e.getKey();
                soleTrack = t;
            }
        }
        if (soleWorld == null || soleTrack.firstSeen < 0 || soleTrack.lastSeen - soleTrack.firstSeen <= DEMO_SINGLE_MIN_MS
                || soleTrack.suspectUntil > now) {
            return false;
        }
        Double evidence = scores.getOrDefault(demo, Map.of()).get(soleWorld);
        if (evidence != null && evidence < 0) {
            return false; // 걸음 대조 반대 근거로 풀린 직후 다시 잇지 않는다
        }
        Long replaceableBindingId = null;
        for (Binding b : bindings.active()) {
            if (soleWorld.equals(b.worldId())) {
                return false; // 대상 트랙에 이미 배정이 있다(시연 워치 본인이거나 다른 워치)
            }
            if (demo.equals(b.deviceId())) {
                if (isTrackVisible(b.worldId(), now)) {
                    return false; // 기존 배정의 World ID 가 지금 보이면 갈아 끼우지 않는다
                }
                replaceableBindingId = b.bindingId();
            }
        }
        if (bindings.bindDemoSingle(demo, soleWorld, replaceableBindingId)) {
            log.info("시연 1명 자동 배정: device={} world_id={} duration_ms={}{}", demo, soleWorld,
                    soleTrack.lastSeen - soleTrack.firstSeen, replaceableBindingId == null ? "" : " (사라진 배정 교체)");
            return true;
        }
        return false;
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
        if (w.rms >= WAVE_RMS) {
            double wave = waveScore(w, t, watch, speed, pelvis);
            if (!Double.isNaN(wave)) {
                return wave;
            }
        }
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

    /**
     * 손 흔들기 체크인(S15P31A101-508): 워치가 크게 흔들리는 창에서 몸통이 멈춰 있는(수평 속력·골반 높이 변화 작음) 트랙의 관측 손목이
     * 크게 움직였으면, 워치 신호와 손목 3D 가속도 크기 |g + a| 를 대역 통과해 밀어 본 상관을 낸다(손목 둘 중 높은 값).
     * 몸통이 멈췄고 손목이 관측되는데 둘 다 안 움직이면 {@link #WAVE_REJECT}. 몸통이 걷거나 관절·골반을 모르면 NaN(걸음 대조가 맡는다).
     */
    private static double waveScore(Window w, Track t, double[] watch, double[] speed, double[] pelvis) {
        if (MotionMatch.coverage(speed) < 0.5 || mean(speed) >= STILL_SPEED || MotionMatch.coverage(pelvis) < 0.6
                || std(pelvis) >= STILL_PELVIS_STD) {
            return Double.NaN;
        }
        int n = watch.length;
        double best = Double.NaN;
        boolean observed = false;
        for (boolean leftHand : new boolean[] {true, false}) {
            double[] x = new double[n];
            double[] y = new double[n];
            double[] z = new double[n];
            for (int k = 0; k < n; k++) {
                Sample s = nearest(t, w.start + Math.round(k * 1000 / MotionMatch.RATE_HZ));
                double[] j = s == null ? NO_JOINT : leftHand ? s.left() : s.right();
                x[k] = j[0];
                y[k] = j[1];
                z[k] = j[2];
            }
            if (MotionMatch.coverage(z) < 0.6) {
                continue;
            }
            observed = true;
            double move = Math.sqrt(sq(MotionMatch.rms(MotionMatch.bandpass(x))) + sq(MotionMatch.rms(MotionMatch.bandpass(y)))
                    + sq(MotionMatch.rms(MotionMatch.bandpass(z))));
            if (Double.isNaN(move) || move < WAVE_MOVE_M) {
                continue;
            }
            double[] accel = MotionMatch.bandpass(MotionMatch.specificForceMagnitude(x, y, z));
            if (MotionMatch.coverage(accel) < 0.6) {
                continue;
            }
            best = max(best, MotionMatch.bestLaggedCorrelation(watch, accel, MAX_LAG));
        }
        if (!Double.isNaN(best)) {
            return best;
        }
        return observed ? WAVE_REJECT : Double.NaN;
    }

    private static double sq(double v) {
        return v * v;
    }

    private static double std(double[] x) {
        double m = mean(x);
        double sum = 0;
        int n = 0;
        for (double v : x) {
            if (!Double.isNaN(v)) {
                sum += (v - m) * (v - m);
                n++;
            }
        }
        return n == 0 ? Double.NaN : Math.sqrt(sum / n);
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
