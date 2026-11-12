package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.lang.reflect.Type;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.Predicate;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.messaging.simp.stomp.StompFrameHandler;
import org.springframework.messaging.simp.stomp.StompHeaders;
import org.springframework.messaging.simp.stomp.StompSession;
import org.springframework.messaging.simp.stomp.StompSessionHandlerAdapter;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.messaging.WebSocketStompClient;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * S15P31A101-658 사람·워치 표시 이름 해석·변경 REST·추가 전용 이력·관제 전달(viewer_health 1.5.0) 통합 시험.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class PersonDisplayNameIntegrationTest {

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    MachineTokenService machineTokens;

    @Autowired
    WatchBindings bindings;

    @Autowired
    PersonDisplayNames displayNames;

    @Autowired
    SiteLookup sites;

    @Autowired
    ApplicationEventPublisher events;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ObjectMapper mapper;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final List<String> testDevices = new ArrayList<>();
    private final List<String> testWorlds = new ArrayList<>();
    private final List<Long> testUsers = new ArrayList<>();

    private String watchA;
    private String watchB;
    private String laptopId;
    private long admin1Id;
    private long admin2Id;
    private long foremanId;
    private long viewerId;
    private String admin1Jwt;
    private String admin2Jwt;
    private String foremanJwt;
    private String viewerJwt;

    @BeforeEach
    void setUp() {
        watchA = registerDevice("watch-name-a-" + tag, "WATCH");
        watchB = registerDevice("watch-name-b-" + tag, "WATCH");
        laptopId = registerDevice("laptop-name-" + tag, "LAPTOP");

        admin1Id = insertUser("name-admin1-" + tag, "ADMIN");
        admin2Id = insertUser("name-admin2-" + tag, "ADMIN");
        foremanId = insertUser("name-foreman-" + tag, "FOREMAN");
        viewerId = insertUser("name-viewer-" + tag, "VIEWER");

        admin1Jwt = issueJwt(admin1Id, "ADMIN");
        admin2Jwt = issueJwt(admin2Id, "ADMIN");
        foremanJwt = issueJwt(foremanId, "FOREMAN");
        viewerJwt = issueJwt(viewerId, "VIEWER");
    }

    @AfterEach
    void tearDown() {
        for (String d : testDevices) {
            jdbc.update("DELETE FROM watch_display_name_history WHERE device_id = ?", d);
            jdbc.update("DELETE FROM watch_binding WHERE device_id = ?", d);
            jdbc.update("DELETE FROM watch_connection WHERE device_id = ?", d);
            jdbc.update("DELETE FROM device WHERE device_id = ?", d);
        }
        for (String w : testWorlds) {
            jdbc.update("DELETE FROM person_world_merge_log WHERE old_world_id = ? OR survivor_world_id = ?", w, w);
            jdbc.update("DELETE FROM person_world_name_history WHERE world_id = ? OR source_world_id = ?", w, w);
            jdbc.update("DELETE FROM person_unidentified_number_history WHERE world_id = ? OR source_world_id = ?", w, w);
            jdbc.update("DELETE FROM watch_binding WHERE world_id = ?", w);
        }
        for (Long u : testUsers) {
            jdbc.update("DELETE FROM app_user WHERE user_id = ?", u);
        }
    }

    @Test
    void REST_권한_입력검증_중복허용_감사이력_추가전용을_지킨다() throws Exception {
        String world1 = trackWorld("w-val-1-" + tag);
        String world2 = trackWorld("w-val-2-" + tag);

        // 1) 인증 없음 401, 열람·반장 403
        assertThat(put("/api/watches/" + watchA + "/display-name", null, Map.of("display_name", "김철수")).statusCode())
                .isEqualTo(401);
        assertThat(put("/api/watches/" + watchA + "/display-name", viewerJwt, Map.of("display_name", "김철수")).statusCode())
                .isEqualTo(403);
        assertThat(put("/api/watches/" + watchA + "/display-name", foremanJwt, Map.of("display_name", "김철수")).statusCode())
                .isEqualTo(403);
        assertThat(put("/api/people/" + world1 + "/display-name", viewerJwt, Map.of("display_name", "김철수")).statusCode())
                .isEqualTo(403);
        assertThat(put("/api/people/" + world1 + "/display-name", foremanJwt, Map.of("display_name", "김철수")).statusCode())
                .isEqualTo(403);

        // 2) 없는 워치·노트북 기기는 404 WATCH_NOT_FOUND
        assertThat(put("/api/watches/watch-missing-" + tag + "/display-name", admin1Jwt,
                Map.of("display_name", "김철수")).statusCode()).isEqualTo(404);
        assertThat(put("/api/watches/" + laptopId + "/display-name", admin1Jwt,
                Map.of("display_name", "김철수")).statusCode()).isEqualTo(404);

        // 3) 입력 검증: 공백만·빈 문자열·21자·제어문자(\n, \t, \u200B)는 400 INVALID_DISPLAY_NAME
        for (String invalid : List.of("", "   ", "가나다라마바사아자차카타파하거너더러머버서", "김\n철수", "김\t철수", "김\u200B철수")) {
            HttpResponse<String> badWatch = put("/api/watches/" + watchA + "/display-name", admin1Jwt,
                    Map.of("display_name", invalid));
            assertThat(badWatch.statusCode()).as("워치 잘못된 이름: " + invalid).isEqualTo(400);
            assertThat(mapper.readTree(badWatch.body()).path("code").asString()).isEqualTo("INVALID_DISPLAY_NAME");

            HttpResponse<String> badWorld = put("/api/people/" + world1 + "/display-name", admin1Jwt,
                    Map.of("display_name", invalid));
            assertThat(badWorld.statusCode()).as("World 잘못된 이름: " + invalid).isEqualTo(400);
            assertThat(mapper.readTree(badWorld.body()).path("code").asString()).isEqualTo("INVALID_DISPLAY_NAME");
        }

        // 4) 앞뒤 공백 제거 + 20자 허용 + 중복 이름 허용 + 감사 기록(누가·언제) + 추가 전용 이력(덮어쓰기 없음)
        HttpResponse<String> first = put("/api/watches/" + watchA + "/display-name", admin1Jwt,
                Map.of("display_name", "  작업자 A  "));
        assertThat(first.statusCode()).isEqualTo(200);
        JsonNode firstJson = mapper.readTree(first.body());
        assertThat(firstJson.path("display_name").asString()).isEqualTo("작업자 A");
        assertThat(firstJson.path("changed_by_user_id").asLong()).isEqualTo(admin1Id);

        HttpResponse<String> second = put("/api/watches/" + watchA + "/display-name", admin2Jwt,
                Map.of("display_name", "김철수"));
        assertThat(second.statusCode()).isEqualTo(200);
        assertThat(mapper.readTree(second.body()).path("changed_by_user_id").asLong()).isEqualTo(admin2Id);

        // 다른 워치·다른 사람에게 같은 이름("김철수") 지정 가능(중복 허용)
        assertThat(put("/api/watches/" + watchB + "/display-name", admin1Jwt,
                Map.of("display_name", "김철수")).statusCode()).isEqualTo(200);
        assertThat(put("/api/people/" + world1 + "/display-name", admin1Jwt,
                Map.of("display_name", "김철수")).statusCode()).isEqualTo(200);
        assertThat(put("/api/people/" + world2 + "/display-name", admin2Jwt,
                Map.of("display_name", "김철수")).statusCode()).isEqualTo(200);

        // 해제(DELETE)도 행을 지우지 않고 CLEAR 행을 추가한다
        assertThat(delete("/api/watches/" + watchA + "/display-name", admin1Jwt).statusCode()).isEqualTo(204);

        List<Map<String, Object>> watchHistory = jdbc.queryForList("""
                SELECT action, display_name, changed_by_user_id
                FROM watch_display_name_history
                WHERE device_id = ?
                ORDER BY history_id ASC
                """, watchA);
        assertThat(watchHistory).hasSize(3);
        assertThat(watchHistory.get(0)).containsEntry("action", "SET")
                .containsEntry("display_name", "작업자 A")
                .containsEntry("changed_by_user_id", admin1Id);
        assertThat(watchHistory.get(1)).containsEntry("action", "SET")
                .containsEntry("display_name", "김철수")
                .containsEntry("changed_by_user_id", admin2Id);
        assertThat(watchHistory.get(2)).containsEntry("action", "CLEAR")
                .containsEntry("display_name", null)
                .containsEntry("changed_by_user_id", admin1Id);
    }

    @Test
    void 우선순위와_현장날짜별_규칙_및_병합승계_분할새번호가_동작한다() {
        String w1 = trackWorld("w-p1-" + tag);
        String w2 = trackWorld("w-p2-" + tag);
        String wMergedSurvivor = trackWorld("w-surv-" + tag);
        String wSplitChild = trackWorld("w-child-" + tag);

        // 1) 처음 관측된 두 사람은 미확인 번호를 순서대로 받고, 같은 프레임이 반복돼도 같은 번호를 유지한다
        publishWorld(trackNode(w1, null, List.of()), trackNode(w2, null, List.of()));
        publishWorld(trackNode(w1, null, List.of()), trackNode(w2, null, List.of()));

        PersonDisplayNames.PersonNameItem p1Initial = resolvedFor(w1);
        PersonDisplayNames.PersonNameItem p2Initial = resolvedFor(w2);
        assertThat(p1Initial.nameSource()).isEqualTo("UNIDENTIFIED");
        assertThat(p2Initial.nameSource()).isEqualTo("UNIDENTIFIED");
        assertThat(p1Initial.displayName()).startsWith("미확인 ");
        assertThat(p2Initial.displayName()).startsWith("미확인 ");
        int seq1 = Integer.parseInt(p1Initial.displayName().substring("미확인 ".length()));
        int seq2 = Integer.parseInt(p2Initial.displayName().substring("미확인 ".length()));
        assertThat(seq2).isEqualTo(seq1 + 1);

        // 2) 워치 표시 이름(현장 지속) > 미확인 N, 관리자 World ID 이름(당일) > 워치 표시 이름
        displayNames.setWatchDisplayName(watchA, "워치박대리", admin1Id);
        bindings.bindManually(watchA, w1, admin1Id); // 워치 WebSocket 연결 전(LOST)이어도 배정된 워치 이름을 쓴다
        assertThat(resolvedFor(w1)).isEqualTo(new PersonDisplayNames.PersonNameItem(w1, "워치박대리", "WATCH_DEVICE"));

        displayNames.setPersonWorldDisplayName(w1, "관리자지정이름", admin2Id);
        assertThat(resolvedFor(w1)).isEqualTo(new PersonDisplayNames.PersonNameItem(w1, "관리자지정이름", "WORLD_MANUAL"));

        // 당일 World ID 이름을 비우면 워치 이름으로, 워치 배정을 풀면 원래 미확인 번호로 돌아간다
        displayNames.clearPersonWorldDisplayName(w1, admin1Id);
        assertThat(resolvedFor(w1)).isEqualTo(new PersonDisplayNames.PersonNameItem(w1, "워치박대리", "WATCH_DEVICE"));

        bindings.release(watchA);
        assertThat(resolvedFor(w1)).isEqualTo(new PersonDisplayNames.PersonNameItem(w1, "미확인 " + seq1, "UNIDENTIFIED"));

        // 3) 다음 날(Asia/Seoul): 워치 기기 표시 이름은 지속되고, World ID 지정 이름·미확인 번호는 그날 한정이다
        displayNames.setPersonWorldDisplayName(w1, "오늘만이름", admin1Id);
        bindings.bindManually(watchA, w2, admin1Id);
        String siteId = sites.siteId();
        LocalDate tomorrow = displayNames.today().plusDays(1);
        PersonDisplayNames.Snapshot nextDay = displayNames.snapshot(siteId, tomorrow);
        assertThat(nextDay.watchNames()).contains(new PersonDisplayNames.WatchNameItem(watchA, "워치박대리"));
        assertThat(nextDay.personNames()).filteredOn(i -> i.worldId().equals(w1)).isEmpty();
        assertThat(nextDay.personNames()).filteredOn(i -> i.worldId().equals(w2))
                .containsExactly(new PersonDisplayNames.PersonNameItem(w2, "워치박대리", "WATCH_DEVICE"));
        bindings.release(watchA);

        // 4) 병합(aliases): w1("오늘만이름", 미확인 seq1) 이 wMergedSurvivor 로 흡수되면 생존 ID 가 이름과 작은 번호를 이어받는다
        publishWorld(trackNode(wMergedSurvivor, null, List.of())); // 먼저 새 번호(seq > seq1)를 받았다고 가정
        int survivorInitialSeq = Integer.parseInt(resolvedFor(wMergedSurvivor).displayName().substring("미확인 ".length()));
        assertThat(survivorInitialSeq).isGreaterThan(seq1);

        publishWorld(trackNode(wMergedSurvivor, null, List.of(w1)));
        publishWorld(trackNode(wMergedSurvivor, null, List.of(w1))); // aliases 반복 전송 멱등 확인
        assertThat(resolvedFor(wMergedSurvivor))
                .isEqualTo(new PersonDisplayNames.PersonNameItem(wMergedSurvivor, "오늘만이름", "WORLD_MANUAL"));
        // 병합 전 발생 이벤트 카드(원본 World ID w1)도 같은 생존자 이름으로 해석된다
        assertThat(resolvedFor(w1))
                .isEqualTo(new PersonDisplayNames.PersonNameItem(w1, "오늘만이름", "WORLD_MANUAL"));

        // 당일 이름을 비우면 생존 ID 는 승계받은 더 작은 번호(seq1)를 유지한다
        displayNames.clearPersonWorldDisplayName(wMergedSurvivor, admin1Id);
        assertThat(resolvedFor(wMergedSurvivor))
                .isEqualTo(new PersonDisplayNames.PersonNameItem(wMergedSurvivor, "미확인 " + seq1, "UNIDENTIFIED"));

        Integer mergeNameRows = jdbc.queryForObject("""
                SELECT count(*) FROM person_world_name_history WHERE world_id = ? AND action = 'MERGE'
                """, Integer.class, wMergedSurvivor);
        assertThat(mergeNameRows).isEqualTo(1);

        // 5) 분할(split_from): 부모(wMergedSurvivor)는 이름을 유지하고 자식(wSplitChild)은 부모 이름 없이 새 번호를 받는다
        displayNames.setPersonWorldDisplayName(wMergedSurvivor, "부모작업자", admin1Id);
        publishWorld(
                trackNode(wMergedSurvivor, null, List.of(w1)),
                trackNode(wSplitChild, wMergedSurvivor, List.of()));

        assertThat(resolvedFor(wMergedSurvivor))
                .isEqualTo(new PersonDisplayNames.PersonNameItem(wMergedSurvivor, "부모작업자", "WORLD_MANUAL"));
        PersonDisplayNames.PersonNameItem childResolved = resolvedFor(wSplitChild);
        assertThat(childResolved.nameSource()).isEqualTo("UNIDENTIFIED");
        int childSeq = Integer.parseInt(childResolved.displayName().substring("미확인 ".length()));
        assertThat(childSeq).isGreaterThan(survivorInitialSeq);
    }

    @Test
    void 관제구독_GET_health_와_STOMP_topic_health_로_계약에_맞는_표시이름을_즉시_전달한다() throws Exception {
        String wLive = trackWorld("w-live-" + tag);
        publishWorld(trackNode(wLive, null, List.of()));

        try (StompViewer viewer = connectViewer(viewerJwt)) {
            assertThat(put("/api/watches/" + watchA + "/display-name", admin1Jwt,
                    Map.of("display_name", "정수연")).statusCode()).isEqualTo(200);
            bindings.bindManually(watchA, wLive, admin1Id);

            JsonNode pushed = viewer.await(msg -> hasPersonName(msg, wLive, "정수연", "WATCH_DEVICE"));
            assertThat(schemas.violations("messages/viewer_health", mapper.writeValueAsString(pushed))).isEmpty();
            assertThat(hasWatchName(pushed, watchA, "정수연")).isTrue();

            HttpResponse<String> snapshotResp = get("/api/health", viewerJwt);
            assertThat(snapshotResp.statusCode()).isEqualTo(200);
            JsonNode snapshot = mapper.readTree(snapshotResp.body());
            assertThat(schemas.violations("messages/viewer_health", snapshotResp.body())).isEmpty();
            assertThat(hasPersonName(snapshot, wLive, "정수연", "WATCH_DEVICE")).isTrue();
            assertThat(hasWatchName(snapshot, watchA, "정수연")).isTrue();
        }
    }

    private PersonDisplayNames.PersonNameItem resolvedFor(String worldId) {
        return displayNames.snapshotForCurrentSite().personNames().stream()
                .filter(i -> i.worldId().equals(worldId))
                .findFirst()
                .orElseThrow(() -> new AssertionError("person_names 에 없음: " + worldId));
    }

    private static boolean hasPersonName(JsonNode healthMsg, String worldId, String expectedName, String expectedSource) {
        for (JsonNode item : healthMsg.at("/payload/person_names")) {
            if (worldId.equals(item.path("world_id").asString(""))
                    && expectedName.equals(item.path("display_name").asString(""))
                    && expectedSource.equals(item.path("name_source").asString(""))) {
                return true;
            }
        }
        return false;
    }

    private static boolean hasWatchName(JsonNode healthMsg, String deviceId, String expectedName) {
        for (JsonNode item : healthMsg.at("/payload/watch_names")) {
            if (deviceId.equals(item.path("device_id").asString(""))
                    && expectedName.equals(item.path("display_name").asString(""))) {
                return true;
            }
        }
        return false;
    }

    private void publishWorld(ObjectNode... tracks) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("t_latest_global", Instant.now().toEpochMilli());
        payload.put("calibration_version", "cal-1");
        payload.put("scene_version", "scene-1");
        ArrayNode arr = payload.putArray("tracks");
        for (ObjectNode t : tracks) {
            arr.add(t);
        }
        I1Envelope env = new I1Envelope("2.1.0", 2, "world_state", UUID.randomUUID(),
                Instant.now(), "tier0-name-" + tag, payload);
        events.publishEvent(new WorldStateStored(env));
    }

    private ObjectNode trackNode(String worldId, String splitFrom, List<String> aliases) {
        ObjectNode t = mapper.createObjectNode();
        t.put("world_id", worldId);
        t.put("track_lifecycle", "CONFIRMED");
        t.put("identity_revision", 1);
        if (splitFrom != null) {
            t.put("split_from", splitFrom);
        }
        if (!aliases.isEmpty()) {
            ArrayNode a = t.putArray("aliases");
            aliases.forEach(a::add);
        }
        return t;
    }

    private String registerDevice(String id, String kind) {
        machineTokens.registerDevice(id, kind);
        testDevices.add(id);
        return id;
    }

    private String trackWorld(String worldId) {
        testWorlds.add(worldId);
        return worldId;
    }

    private long insertUser(String loginId, String role) {
        Long id = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, role, created_at)
                VALUES (?, 'x', ?, now()) RETURNING user_id
                """, Long.class, loginId, role);
        long userId = id == null ? -1 : id;
        testUsers.add(userId);
        return userId;
    }

    private String issueJwt(long userId, String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer(JwtConfig.ISSUER)
                .subject(Long.toString(userId))
                .issuedAt(now)
                .expiresAt(now.plusSeconds(600))
                .claim(AccessTokenIssuer.ROLE_CLAIM, role)
                .build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims))
                .getTokenValue();
    }

    private HttpResponse<String> get(String path, String jwt) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path)).GET();
        if (jwt != null) {
            b.header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt);
        }
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private HttpResponse<String> put(String path, String jwt, Map<String, Object> body) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header(HttpHeaders.CONTENT_TYPE, "application/json")
                .PUT(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)));
        if (jwt != null) {
            b.header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt);
        }
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private HttpResponse<String> delete(String path, String jwt) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path)).DELETE();
        if (jwt != null) {
            b.header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt);
        }
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private StompViewer connectViewer(String jwt) throws Exception {
        WebSocketStompClient client = new WebSocketStompClient(new StandardWebSocketClient());
        BlockingQueue<JsonNode> queue = new LinkedBlockingQueue<>();
        StompHeaders connectHeaders = new StompHeaders();
        connectHeaders.add(HttpHeaders.AUTHORIZATION, "Bearer " + jwt);
        StompSession session = client.connectAsync(URI.create("ws://localhost:" + port + RealtimeTopics.ENDPOINT),
                new WebSocketHttpHeaders(), connectHeaders, new StompSessionHandlerAdapter() {
                }).get(5, TimeUnit.SECONDS);
        session.subscribe(RealtimeTopics.HEALTH, new StompFrameHandler() {
            @Override
            public Type getPayloadType(StompHeaders headers) {
                return byte[].class;
            }

            @Override
            public void handleFrame(StompHeaders headers, Object payload) {
                queue.offer(mapper.readTree((byte[]) payload));
            }
        });
        return new StompViewer(client, session, queue);
    }

    private record StompViewer(WebSocketStompClient client, StompSession session, BlockingQueue<JsonNode> queue)
            implements AutoCloseable {

        JsonNode await(Predicate<JsonNode> predicate) throws InterruptedException {
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
            while (System.nanoTime() < deadline) {
                JsonNode next = queue.poll(500, TimeUnit.MILLISECONDS);
                if (next != null && predicate.test(next)) {
                    return next;
                }
            }
            throw new AssertionError("기한 안에 조건에 맞는 /topic/health 메시지를 받지 못함");
        }

        @Override
        public void close() {
            if (session.isConnected()) {
                session.disconnect();
            }
            client.stop();
        }
    }
}
