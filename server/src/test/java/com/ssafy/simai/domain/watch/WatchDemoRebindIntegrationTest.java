package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.net.URI;
import java.sql.Timestamp;
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
import org.springframework.security.crypto.password.PasswordEncoder;
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
 * S15P31A101-826 시연 워치 자동 다시 잇기 통합 시험(2차: 시간으로 풀지 않고, 배정 ID 가 안 보일 때만 새 후보가 REPLACED 로 갈아 끼움).
 * <ul>
 * <li>11-19 시연 재현: 사라진 ID 의 수동 배정이 있어도 작업 시작 전 1명 트랙이 5초 넘게 이어지면 시연 워치가 새 ID 로
 *     DEMO_SINGLE 갈아 끼워지고(옛 행 REPLACED), 표시 이름·배정 알림·경보 중복 없음을 지킨다</li>
 * <li>사라졌던 ID 는 시간이 오래(2분) 지나도 풀리지 않으며, 갈아 끼우기 전에 Re-ID 등으로 다시 보이면 원래 배정이 유지된다</li>
 * <li>사라진 지 10초(TRACK_GONE_MS) 이내에는 갈아 끼우지 않고, 10초가 지나 보이지 않게 된 뒤에 갈아 끼운다</li>
 * <li>2명 이상이거나, 트랙이 5초 이하이거나, 시연 스위치가 꺼져 있으면 시연 잇기·갈아 끼우기를 하지 않는다</li>
 * <li>시연 스위치가 꺼져 있어도 배정 ID 가 안 보이는 동안 걸음 대조가 맞는 새 후보를 찾으면 수동 배정도 MOTION_MATCH 로 갈아 끼운다</li>
 * <li>DEMO_SINGLE 배정은 임시 성격이라 걸음 대조 반대 근거로 MATCH_LOST 해제되며 바로 재결합하지 않고,
 *     같은 리듬으로 걸으면 MOTION_MATCH 로 승격된다</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class WatchDemoRebindIntegrationTest {

    private static final int FEED_BATCH = WatchMotionMatcher.MAX_PENDING_WORLD / 2;
    private static final String PASSWORD = "demo-rebind-password";

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
    WatchBindingNotifier notifier;

    @Autowired
    WatchAlertService alerts;

    @Autowired
    PersonDisplayNames displayNames;

    @Autowired
    AppUserRepository users;

    @Autowired
    PasswordEncoder passwords;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    ObjectMapper mapper;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final List<AutoCloseable> closeables = new ArrayList<>();
    private String watchDemo;
    private String tokenDemo;
    private String watchOther;
    private String tokenOther;
    private String adminLogin;
    private long adminId;
    private String gateway;
    private long t0;

    @BeforeEach
    void setUp() throws Exception {
        matcher.resetForTest();
        alerts.setDemoDevice(null);
        watchDemo = "watch-dr-1-" + tag;
        watchOther = "watch-dr-2-" + tag;
        tokenDemo = machineTokens.registerDevice(watchDemo, "WATCH").token();
        tokenOther = machineTokens.registerDevice(watchOther, "WATCH").token();
        adminLogin = "demo-rebind-admin-" + tag;
        AppUser saved = users.saveAndFlush(new AppUser(adminLogin, passwords.encode(PASSWORD), Role.ADMIN, null, Instant.now()));
        adminId = saved.getId();
        gateway = "tier0-dr-" + tag;
        // 200초 전을 기준점으로 삼아 +150초 이상 흘려도 MAX_FUTURE_MS 에 걸리지 않게 한다
        t0 = (System.currentTimeMillis() / 100) * 100 - 200_000;
    }

    @AfterEach
    void tearDown() throws Exception {
        matcher.setDemoDevice(null);
        alerts.setDemoDevice(null);
        for (AutoCloseable c : closeables) {
            try {
                c.close();
            } catch (Exception e) {
                // 이미 닫힘
            }
        }
        for (String w : List.of(watchDemo, watchOther)) {
            waitUntil(() -> count("SELECT count(*) FROM watch_connection WHERE device_id = ? AND ended_at IS NULL", w) == 0, 3_000);
            jdbc.update("DELETE FROM watch_alert WHERE device_id = ?", w);
            jdbc.update("DELETE FROM watch_binding WHERE device_id = ?", w);
            jdbc.update("DELETE FROM watch_feature WHERE device_id = ?", w);
            jdbc.update("DELETE FROM watch_connection WHERE device_id = ?", w);
            jdbc.update("DELETE FROM watch_display_name_history WHERE device_id = ?", w);
            jdbc.update("DELETE FROM device WHERE device_id = ?", w);
        }
        jdbc.update("DELETE FROM risk_event WHERE gateway_id = ?", gateway);
        String like = "wdr-%-" + tag;
        jdbc.update("DELETE FROM channel_state WHERE world_id LIKE ?", like);
        jdbc.update("DELETE FROM person_world_merge_log WHERE old_world_id LIKE ? OR survivor_world_id LIKE ?", like, like);
        jdbc.update("DELETE FROM person_unidentified_number_history WHERE world_id LIKE ?", like);
        jdbc.update("DELETE FROM person_world_name_history WHERE world_id LIKE ?", like);
        jdbc.update("DELETE FROM refresh_token WHERE user_id = ?", adminId);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
        matcher.resetForTest();
    }

    @Test
    void 사라진_수동_배정이_있어도_1명_트랙_5초_뒤_시연_워치가_갈아_끼워지고_표시이름과_경보_중복_없음을_지킨다() throws Exception {
        // 11-19 시연 재현: 리허설 수동 배정(oldId)이 사라진 상태에서 작업 시작 전 배우 1명(newId)이 5초 넘게 서 있다
        String oldId = world("old");
        String newId = world("new");
        displayNames.setWatchDisplayName(watchDemo, "시연작업자", adminId);
        bindings.bindManually(watchDemo, oldId, adminId);
        Watch watch = connect(watchDemo, tokenDemo);
        JsonNode initialNotice = watch.next("watch_binding_notice");
        assertThat(initialNotice.path("payload").path("world_id").asString()).isEqualTo(oldId);

        // 리허설 시각(t=0~1초)에 oldId 가 보인 뒤 사라지고, 15초 뒤(t=16~22초, >10초 미관측) 작업 시작 전 배우 1명이 6초 서 있다
        feed(0, 1_000, Person.still(oldId, 1.0, 1.0));
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).as("관측 직후 확정 수동 배정은 그대로").isEqualTo(oldId);

        matcher.setDemoDevice(watchDemo);
        alerts.setDemoDevice(watchDemo);
        feed(16_000, 22_000, Person.still(newId, 2.0, 2.0));
        matcher.matchNow();

        // 1) 옛 수동 배정은 지우지 않고 REPLACED 로 닫히고, newId 에 DEMO_SINGLE 로 갈아 끼워진다
        assertThat(jdbc.queryForList("""
                SELECT method || ':' || coalesce(unbind_reason, '-') FROM watch_binding
                WHERE device_id = ? ORDER BY binding_id
                """, String.class, watchDemo)).containsExactly("MANUAL:REPLACED", "DEMO_SINGLE:-");
        assertThat(activeWorld(watchDemo)).isEqualTo(newId);

        // 2) 한 트랜잭션으로 교체돼 중간 UNBOUND 없이 바로 newId BOUND 알림이 계약 검사를 지나 도착한다
        JsonNode rebound = watch.next("watch_binding_notice");
        assertThat(schemas.violations("messages/watch_binding_notice", rebound.toString())).isEmpty();
        assertThat(rebound.path("payload").path("state").asString()).isEqualTo("BOUND");
        assertThat(rebound.path("payload").path("world_id").asString()).isEqualTo(newId);
        assertThat(rebound.path("payload").path("method").asString()).isEqualTo("MOTION_MATCH");

        // 3) 채널 상태 BOUND 및 표시 이름 해석(WATCH_DEVICE "시연작업자")
        assertThat(jdbc.queryForObject("""
                SELECT state FROM channel_state WHERE world_id = ? ORDER BY observed_at DESC LIMIT 1
                """, String.class, newId)).isEqualTo("BOUND");
        PersonDisplayNames.Snapshot names = displayNames.snapshotForCurrentSite();
        assertThat(names.personNames()).anySatisfy(p -> {
            assertThat(p.worldId()).isEqualTo(newId);
            assertThat(p.displayName()).isEqualTo("시연작업자");
            assertThat(p.nameSource()).isEqualTo(PersonDisplayNames.SOURCE_WATCH_DEVICE);
        });

        // 4) 시연 스위치와 DEMO_SINGLE 배정이 함께 켜져 있어도 LIVE 경보는 한 번만 발송된다
        UUID eventId = insertLiveEvent("R9", "CAUTION", newId);
        alerts.alert(eventId);
        alerts.alert(eventId);
        JsonNode alertMsg = watch.next("watch_alert");
        assertThat(alertMsg.path("payload").path("event_id").asString()).isEqualTo(eventId.toString());
        assertThat(watch.none("watch_alert")).as("중복 경보 없음").isTrue();
        assertThat(count("SELECT count(*) FROM watch_alert WHERE event_id = ? AND device_id = ?", eventId, watchDemo)).isEqualTo(1);
    }

    @Test
    void 사라졌던_ID_는_시간이_지나도_풀리지_않고_갈아_끼우기_전에_다시_보이면_원래_배정이_유지된다() throws Exception {
        String workerId = world("reid");
        String visitorA = world("vis-a");
        String visitorB = world("vis-b");
        displayNames.setWatchDisplayName(watchDemo, "김철수", adminId);
        long bindingId = bindings.bindManually(watchDemo, workerId, adminId).bindingId();
        Watch watch = connect(watchDemo, tokenDemo);
        watch.next("watch_binding_notice"); // 초기 BOUND

        // t=0~1초 관측 뒤 현장을 2분(120초) 동안 떠남. 그사이 2명이 있어 갈아 끼우지 않는다
        feed(0, 1_000, Person.still(workerId, 1.0, 1.0));
        matcher.setDemoDevice(watchDemo);
        feed(121_000, 128_000, Person.still(visitorA, 2.0, 2.0), Person.still(visitorB, 4.0, 2.0));
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).as("2분 지나도 갈아 끼워지지 않으면 수동 배정 유지").isEqualTo(workerId);
        assertThat(watch.none("watch_binding_notice")).as("불필요한 UNBOUND 알림 없음").isTrue();

        // t=130~137초: Re-ID 가 같은 workerId 로 복귀(이제 화면에 혼자 7초 서 있음) → 원래 수동 배정 행 그대로 유지
        feed(130_000, 137_000, Person.still(workerId, 1.2, 1.1));
        matcher.matchNow();
        assertThat(bindings.activeForDevice(watchDemo)).isPresent().get().satisfies(b -> {
            assertThat(b.bindingId()).isEqualTo(bindingId);
            assertThat(b.worldId()).isEqualTo(workerId);
            assertThat(b.method()).isEqualTo("MANUAL");
        });
        PersonDisplayNames.Snapshot names = displayNames.snapshotForCurrentSite();
        assertThat(names.personNames()).anySatisfy(p -> {
            assertThat(p.worldId()).isEqualTo(workerId);
            assertThat(p.displayName()).isEqualTo("김철수");
            assertThat(p.nameSource()).isEqualTo(PersonDisplayNames.SOURCE_WATCH_DEVICE);
        });
    }

    @Test
    void 배정된_ID_가_10초_이내_잠깐_가려진_동안에는_갈아_끼우지_않고_10초가_지나면_갈아_끼운다() throws Exception {
        String oldId = world("brief-old");
        String newId = world("brief-new");
        bindings.bindManually(watchDemo, oldId, adminId);
        matcher.setDemoDevice(watchDemo);

        // t=0~1초 oldId 관측 뒤 가려짐, t=2~8초 newId 혼자 6초 보임(oldId 마지막 관측 t=900 → 7초 경과, 10초 이내)
        feed(0, 1_000, Person.still(oldId, 0.5, 0.5));
        feed(2_000, 8_000, Person.still(newId, 3.0, 3.0));
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).as("10초(TRACK_GONE_MS) 이내 가려짐은 갈아 끼우지 않음").isEqualTo(oldId);

        // t=8~12초: 이제 oldId 마지막 관측으로부터 11초 경과(>10초) → newId 로 갈아 끼워진다
        feed(8_000, 12_000, Person.still(newId, 3.0, 3.0));
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).isEqualTo(newId);
        assertThat(jdbc.queryForList("""
                SELECT method || ':' || coalesce(unbind_reason, '-') FROM watch_binding
                WHERE device_id = ? ORDER BY binding_id
                """, String.class, watchDemo)).containsExactly("MANUAL:REPLACED", "DEMO_SINGLE:-");
    }

    @Test
    void 스위치가_꺼져_있거나_5초_이하이거나_2명_이상이면_시연_잇기와_갈아_끼우기를_하지_않는다() throws Exception {
        String goneId = world("gone");
        String p1 = world("p1");
        String p2 = world("p2");

        // 1) 스위치 꺼짐 + 사라진 수동 배정 + 1명 6초 → 시연 잇기 없음, 기존 배정 유지
        bindings.bindManually(watchDemo, goneId, adminId);
        matcher.setDemoDevice(null);
        feed(0, 6_000, Person.still(p1, 1.0, 1.0));
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).as("스위치 꺼짐이면 갈아 끼우지 않음").isEqualTo(goneId);

        // 2) 스위치 켜짐 + 1명 4.5초(5초 이하) → 갈아 끼우지 않음
        matcher.resetForTest();
        matcher.setDemoDevice(watchDemo);
        feed(0, 4_500, Person.still(p1, 1.0, 1.0));
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).isEqualTo(goneId);

        // 3) 2명 이상 6초 → 갈아 끼우지 않음(걸음 대조에 맡김)
        feed(4_500, 6_500, Person.still(p1, 1.0, 1.0), Person.still(p2, 3.0, 1.0));
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).isEqualTo(goneId);

        // 4) p2 가 사라져(3초 초과) p1 한 명만 남으면(이미 5초 초과) 갈아 끼운다
        feed(6_500, 10_000, Person.still(p1, 1.0, 1.0));
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).isEqualTo(p1);
        assertThat(jdbc.queryForList("""
                SELECT method || ':' || coalesce(unbind_reason, '-') FROM watch_binding
                WHERE device_id = ? ORDER BY binding_id
                """, String.class, watchDemo)).containsExactly("MANUAL:REPLACED", "DEMO_SINGLE:-");
    }

    @Test
    void 시연_스위치가_꺼져_있어도_사라진_수동_배정은_걸음_대조가_맞는_새_후보로_갈아_끼운다() throws Exception {
        // 시연 스위치 OFF. oldId 에 수동 확정 배정이 있지만 10초 넘게 안 보이고, 새 걷는 사람(walker)의 리듬이 워치와 맞는다
        String oldId = world("manual-gone");
        String walkId = world("walker");
        bindings.bindManually(watchDemo, oldId, adminId);
        Watch watch = connect(watchDemo, tokenDemo);
        watch.next("watch_binding_notice"); // 초기 oldId BOUND

        matcher.setDemoDevice(null);
        Person walker = Person.walking(walkId, 2.0, 2.0, 1.8, 0.4);
        for (long start = 20_000; start + 4_000 <= 34_000; start += 2_000) {
            watch.send(gait(watchDemo, t0 + start, 1.8, 0.4, true));
        }
        feed(20_000, 34_000, walker);
        assertThat(waitUntil(() -> count("SELECT count(*) FROM watch_feature WHERE device_id = ?", watchDemo) >= 6, 15_000))
                .as("걸음 특징 저장").isTrue();
        matcher.matchNow();

        assertThat(activeWorld(watchDemo)).isEqualTo(walkId);
        assertThat(jdbc.queryForList("""
                SELECT method || ':' || coalesce(unbind_reason, '-') FROM watch_binding
                WHERE device_id = ? ORDER BY binding_id
                """, String.class, watchDemo)).containsExactly("MANUAL:REPLACED", "MOTION_MATCH:-");
        JsonNode notice = watch.next("watch_binding_notice");
        assertThat(notice.path("payload").path("state").asString()).isEqualTo("BOUND");
        assertThat(notice.path("payload").path("world_id").asString()).isEqualTo(walkId);
        assertThat(notice.path("payload").path("method").asString()).isEqualTo("MOTION_MATCH");
    }

    @Test
    void 시연_자동_배정은_임시_성격이라_반대_근거로_풀리고_재결합하지_않으며_걸음이_맞으면_MOTION_MATCH_로_승격된다() throws Exception {
        // 1) 반대 근거로 풀림: 서 있는 사람에게 DEMO_SINGLE 배정 후 워치가 걷는 창을 보내면 MATCH_LOST 로 풀리고 다시 붙지 않는다
        Watch watch = connect(watchDemo, tokenDemo);
        watch.next("watch_binding_notice"); // 초기 UNBOUND
        matcher.setDemoDevice(watchDemo);
        String stillId = world("still");
        feed(0, 6_000, Person.still(stillId, 1.0, 1.0));
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).isEqualTo(stillId);
        watch.next("watch_binding_notice"); // BOUND

        for (long start = 6_000; start + 4_000 <= 14_000; start += 2_000) {
            watch.send(gait(watchDemo, t0 + start, 1.8, 0.0, true));
        }
        feed(6_000, 14_000, Person.still(stillId, 1.0, 1.0));
        assertThat(waitUntil(() -> count("SELECT count(*) FROM watch_feature WHERE device_id = ?", watchDemo) >= 3, 15_000))
                .as("걸음 특징 저장").isTrue();
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).as("반대 근거로 풀리고 바로 재결합하지 않음").isNull();
        assertThat(jdbc.queryForList("""
                SELECT method || ':' || coalesce(unbind_reason, '-') FROM watch_binding WHERE device_id = ? ORDER BY binding_id
                """, String.class, watchDemo)).containsExactly("DEMO_SINGLE:MATCH_LOST");
        // 한 번 더 돌려도 음수 점수가 남아 있는 동안 다시 잇지 않는다
        matcher.matchNow();
        assertThat(activeWorld(watchDemo)).isNull();

        // 2) 같은 리듬으로 걸으면 MOTION_MATCH 로 승격된다
        matcher.resetForTest();
        matcher.setDemoDevice(watchOther);
        Watch watch2 = connect(watchOther, tokenOther);
        watch2.next("watch_binding_notice"); // 초기 UNBOUND
        String walkId = world("walk");
        Person walker = Person.walking(walkId, 2.0, 2.0, 1.9, 0.7);
        feed(20_000, 26_000, walker);
        matcher.matchNow();
        assertThat(activeWorld(watchOther)).isEqualTo(walkId);
        assertThat(watch2.next("watch_binding_notice").path("payload").path("state").asString()).isEqualTo("BOUND");

        for (long start = 26_000; start + 4_000 <= 40_000; start += 2_000) {
            watch2.send(gait(watchOther, t0 + start, 1.9, 0.7, true));
        }
        feed(26_000, 40_000, walker);
        assertThat(waitUntil(() -> count("SELECT count(*) FROM watch_feature WHERE device_id = ?", watchOther) >= 6, 15_000))
                .as("워치2 걸음 특징 저장").isTrue();
        matcher.matchNow();
        assertThat(jdbc.queryForList("""
                SELECT method || ':' || coalesce(unbind_reason, '-') FROM watch_binding WHERE device_id = ? ORDER BY binding_id
                """, String.class, watchOther)).containsExactly("DEMO_SINGLE:REPLACED", "MOTION_MATCH:-");
        assertThat(watch2.none("watch_binding_notice")).as("같은 World ID 승격은 알림 반복 없음").isTrue();
    }

    // ------------------------------------------------------------------ 도우미

    private String world(String name) {
        return "wdr-" + name + "-" + tag;
    }

    private String activeWorld(String deviceId) {
        return bindings.activeForDevice(deviceId).map(WatchBindings.Binding::worldId).orElse(null);
    }

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
            double wristZ = moving ? 1.0 + Math.sin(w * sec + person.phase()) / (w * w) : 1.0;
            ObjectNode tr = tracks.addObject();
            tr.put("world_id", person.worldId());
            tr.put("track_lifecycle", "CONFIRMED");
            tr.put("identity_revision", 0);
            tr.putArray("position_xy").add(person.x()).add(person.y());
            tr.putArray("velocity").add(moving ? 1.0 : 0.0).add(0.0).add(0.0);
            tr.putArray("pelvis_xyz").add(person.x()).add(person.y()).add(0.95);
            ObjectNode sk = tr.putObject("skeleton");
            sk.put("joint_layout", "COCO17");
            ArrayNode joints = sk.putArray("joints");
            ArrayNode validity = sk.putArray("validity");
            ArrayNode source = sk.putArray("joint_source");
            for (int j = 0; j < 17; j++) {
                joints.addArray().add(person.x()).add(person.y()).add(j == 9 || j == 10 ? wristZ : 1.2);
                validity.add("VALID");
                source.add("observed");
            }
        }
        return new WorldStateStored(new I1Envelope("2.0.0", 2, "world_state", UUID.randomUUID(), Instant.ofEpochMilli(t),
                "tier0-test", p));
    }

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

    private UUID insertLiveEvent(String ruleId, String severity, String worldId) {
        UUID id = UUID.randomUUID();
        Instant now = Instant.now();
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification,
                    severity, confidence, needs_check, world_ids, metrics, resume_count, event_sequence,
                    raised_at, updated_at, delivery, retention_hold)
                VALUES (?, 'LIVE', ?, ?, 1, 'RAISED', 'VERIFIED', ?, 'HIGH', false, ARRAY[?]::text[], '{}', 0, 1,
                    ?, ?, 'REALTIME', false)
                """, id, gateway, ruleId, severity, worldId, Timestamp.from(now), Timestamp.from(now));
        return id;
    }

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

    private final class Watch implements AutoCloseable {
        final BlockingQueue<JsonNode> incoming = new LinkedBlockingQueue<>();
        final ObjectMapper mapper;
        WebSocketSession session;
        final TextWebSocketHandler handler = new TextWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                JsonNode m = Watch.this.mapper.readTree(message.getPayload());
                if (!"watch_heartbeat".equals(m.path("msg_type").asString())) {
                    incoming.add(m);
                }
            }
        };

        Watch(ObjectMapper mapper) {
            this.mapper = mapper;
        }

        JsonNode next(String msgType) throws Exception {
            notifier.awaitIdle();
            long deadline = System.nanoTime() + 5_000_000_000L;
            while (System.nanoTime() < deadline) {
                JsonNode m = incoming.poll(200, TimeUnit.MILLISECONDS);
                if (m != null && msgType.equals(m.path("msg_type").asString())) {
                    return m;
                }
            }
            throw new AssertionError(msgType + " 메시지가 5초 안에 오지 않았다");
        }

        boolean none(String msgType) throws Exception {
            notifier.awaitIdle();
            long deadline = System.nanoTime() + 400_000_000L;
            while (System.nanoTime() < deadline) {
                JsonNode m = incoming.poll(100, TimeUnit.MILLISECONDS);
                if (m != null && msgType.equals(m.path("msg_type").asString())) {
                    return false;
                }
            }
            return true;
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
