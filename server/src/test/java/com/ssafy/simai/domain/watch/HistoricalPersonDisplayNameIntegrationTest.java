package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * S15P31A101-835 지난 기록용 시각별 사람 표시 이름 조회 API(GET /api/people/display-names?date=YYYY-MM-DD) 통합 시험.
 * <ul>
 *   <li>권한(인증 없음 401, 열람·반장·관리자 200) 및 날짜 형식 검증(400 INVALID_DISPLAY_NAME_DATE)</li>
 *   <li>시각별 워치 배정·해제·재배정과 워치 표시 이름 변경·초기화·다음 날 변경 미반영</li>
 *   <li>우선순위(World 당일 지정 이름 &gt; 그 시각 배정 워치 이름 &gt; 없음)와 당일 지정 해제 시 워치 이름 복귀</li>
 *   <li>병합(aliases)·자리 이어받기(handoff_from) 승계와 분할(split_from) 미승계</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class HistoricalPersonDisplayNameIntegrationTest {

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    MachineTokenService machineTokens;

    @Autowired
    SiteLookup sites;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ObjectMapper mapper;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final List<String> testDevices = new ArrayList<>();
    private final List<String> testWorlds = new ArrayList<>();
    private final List<Long> testUsers = new ArrayList<>();

    private String siteId;
    private String watchA;
    private String watchB;
    private long adminId;
    private long viewerId;
    private String viewerJwt;

    @BeforeEach
    void setUp() {
        siteId = sites.siteId();
        watchA = registerDevice("watch-hist-a-" + tag);
        watchB = registerDevice("watch-hist-b-" + tag);
        adminId = insertUser("hist-admin-" + tag, "ADMIN");
        viewerId = insertUser("hist-viewer-" + tag, "VIEWER");
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
    void 인증_권한과_날짜_형식_검증을_지킨다() throws Exception {
        assertThat(get("/api/people/display-names?date=2025-05-10", null).statusCode()).isEqualTo(401);

        for (String badPath : List.of(
                "/api/people/display-names",
                "/api/people/display-names?date=",
                "/api/people/display-names?date=2025-5-1",
                "/api/people/display-names?date=2025-02-30",
                "/api/people/display-names?date=invalid")) {
            HttpResponse<String> bad = get(badPath, viewerJwt);
            assertThat(bad.statusCode()).as("잘못된 요청: " + badPath).isEqualTo(400);
            assertThat(mapper.readTree(bad.body()).path("code").asString()).isEqualTo("INVALID_DISPLAY_NAME_DATE");
        }

        // 열람(VIEWER)도 읽기 전용 조회 가능, 기록이 없는 날짜는 빈 items 반환
        HttpResponse<String> empty = get("/api/people/display-names?date=2024-01-15", viewerJwt);
        assertThat(empty.statusCode()).isEqualTo(200);
        JsonNode body = mapper.readTree(empty.body());
        assertThat(body.path("site_id").asString()).isEqualTo(siteId);
        assertThat(body.path("date").asString()).isEqualTo("2024-01-15");
        assertThat(body.path("items").isArray()).isTrue();
    }

    @Test
    void 시각별_워치배정_이름변경_우선순위_다음날변경미반영을_정확히_해석한다() throws Exception {
        LocalDate day = LocalDate.of(2025, 6, 10);
        String w1 = trackWorld("w-h1-" + tag);
        String w2 = trackWorld("w-h2-" + tag);
        String wUnnamed = trackWorld("w-hu-" + tag);

        // 전날 지정한 워치 이름(현장 지속) + 당일 이름 변경·초기화 + 다음 날 이름 변경(당일에 영향 없어야 함)
        insertWatchName(watchA, "SET", "추창만", kst(day.minusDays(1), 8, 0));
        insertWatchName(watchA, "SET", "추반장", kst(day, 10, 0));
        insertWatchName(watchA, "CLEAR", null, kst(day, 11, 0));
        insertWatchName(watchA, "SET", "다음날이름", kst(day.plusDays(1), 9, 0));
        insertWatchName(watchB, "SET", "이영희", kst(day, 8, 30));

        // 09:00~11:30 watchA -> w1, 11:30~12:00 watchB -> w1 (REPLACED), 12:00 해제(RELEASED)
        insertBinding(watchA, w1, kst(day, 9, 0), kst(day, 11, 30), "REPLACED", null, null);
        insertBinding(watchB, w1, kst(day, 11, 30), kst(day, 12, 0), "RELEASED", null, null);
        // 11:30~12:30 watchA -> w2 (단, watchA 는 11:00 CLEAR 상태라 이름 없음)
        insertBinding(watchA, w2, kst(day, 11, 30), kst(day, 12, 30), "RELEASED", null, null);

        // 09:30~10:30 w1 에 World 수동 지정 이름(워치 이름보다 우선) 후 10:30 CLEAR -> 워치 이름("추반장")으로 복귀
        insertWorldName(day, w1, "SET", "임시지정", null, kst(day, 9, 30));
        insertWorldName(day, w1, "CLEAR", null, null, kst(day, 10, 30));

        // wUnnamed 는 미확인 번호만 있고 이름은 없음 -> 응답에 나오지 않아야 한다
        jdbc.update("""
                INSERT INTO person_unidentified_number_history (site_id, site_date, world_id, seq_no, reason, created_at)
                VALUES (?, ?, ?, 1, 'INITIAL', ?)
                """, siteId, day, wUnnamed, utc(kst(day, 9, 0)));

        JsonNode items = fetchItems(day);

        // w1 시각별 확인
        assertThat(resolveAt(items, w1, kst(day, 8, 59))).isNull();
        assertThat(resolveAt(items, w1, kst(day, 9, 15))).isEqualTo("추창만|WATCH_DEVICE");
        assertThat(resolveAt(items, w1, kst(day, 9, 45))).isEqualTo("임시지정|WORLD_MANUAL");
        assertThat(resolveAt(items, w1, kst(day, 10, 15))).isEqualTo("임시지정|WORLD_MANUAL");
        assertThat(resolveAt(items, w1, kst(day, 10, 45))).isEqualTo("추반장|WATCH_DEVICE");
        assertThat(resolveAt(items, w1, kst(day, 11, 15))).isNull(); // 11:00 watchA CLEAR
        assertThat(resolveAt(items, w1, kst(day, 11, 45))).isEqualTo("이영희|WATCH_DEVICE");
        assertThat(resolveAt(items, w1, kst(day, 12, 5))).isNull(); // 12:00 RELEASED

        // w2 (이름이 비워진 워치)·wUnnamed (미확인 번호만)는 이름 없음
        assertThat(resolveAt(items, w2, kst(day, 12, 0))).isNull();
        assertThat(resolveAt(items, wUnnamed, kst(day, 9, 30))).isNull();
    }

    @Test
    void 병합_aliases_와_자리이어받기_handoff_from_승계를_반영한다() throws Exception {
        LocalDate day = LocalDate.of(2025, 6, 11);
        String wOld = trackWorld("w-merge-old-" + tag);
        String wSurv = trackWorld("w-merge-surv-" + tag);
        String wHoOld = trackWorld("w-ho-old-" + tag);
        String wHoNew = trackWorld("w-ho-new-" + tag);
        String wSplitChild = trackWorld("w-split-" + tag);

        // 1) 병합(aliases): wOld 에 당일 수동 이름 "김철수" 지정 -> 10:00 wOld -> wSurv 병합 -> 10:30 wSurv 에서 CLEAR
        insertWorldName(day, wOld, "SET", "김철수", null, kst(day, 9, 0));
        insertMergeLog(day, wOld, wSurv, kst(day, 10, 0));
        insertWorldName(day, wSurv, "MERGE", "김철수", wOld, kst(day, 10, 0));
        insertWorldName(day, wSurv, "CLEAR", null, null, kst(day, 10, 30));

        // 2) 자리 이어받기(handoff_from): watchA("박민수")가 09:00~09:30 wHoOld 에 배정됐다가 09:30~10:30 wHoNew 로 이어받기
        //    (person_world_merge_log 없이 watch_binding.handoff_from 만 있어도 승계돼야 한다)
        insertWatchName(watchA, "SET", "박민수", kst(day, 8, 0));
        insertWatchName(watchA, "SET", "박반장", kst(day, 10, 0));
        insertBinding(watchA, wHoOld, kst(day, 9, 0), kst(day, 9, 30), "HANDOFF", null, null);
        insertBinding(watchA, wHoNew, kst(day, 9, 30), kst(day, 10, 30), "RELEASED", wHoOld, kst(day, 9, 35));

        // 3) 분할 자식은 계보에 없으므로 이름 없음
        jdbc.update("""
                INSERT INTO person_unidentified_number_history
                    (site_id, site_date, world_id, seq_no, reason, source_world_id, created_at)
                VALUES (?, ?, ?, 2, 'SPLIT', ?, ?)
                """, siteId, day, wSplitChild, wSurv, utc(kst(day, 10, 5)));

        JsonNode items = fetchItems(day);

        // 병합 전·후 모두 같은 사람 이름으로 해석되고, 10:30 CLEAR 이후에는 양쪽 모두 비워진다
        assertThat(resolveAt(items, wOld, kst(day, 9, 30))).isEqualTo("김철수|WORLD_MANUAL");
        assertThat(resolveAt(items, wSurv, kst(day, 9, 30))).isEqualTo("김철수|WORLD_MANUAL");
        assertThat(resolveAt(items, wSurv, kst(day, 10, 15))).isEqualTo("김철수|WORLD_MANUAL");
        assertThat(resolveAt(items, wOld, kst(day, 10, 15))).isEqualTo("김철수|WORLD_MANUAL");
        assertThat(resolveAt(items, wSurv, kst(day, 10, 45))).isNull();
        assertThat(resolveAt(items, wOld, kst(day, 10, 45))).isNull();

        // 자리 이어받기: 이전 ID·새 ID 모두 그 시각 워치 이름으로 해석되고, 10:00 이름 변경·10:30 해제도 반영된다
        assertThat(resolveAt(items, wHoOld, kst(day, 9, 15))).isEqualTo("박민수|WATCH_DEVICE");
        assertThat(resolveAt(items, wHoNew, kst(day, 9, 29))).isEqualTo("박민수|WATCH_DEVICE");
        assertThat(resolveAt(items, wHoNew, kst(day, 9, 45))).isEqualTo("박민수|WATCH_DEVICE");
        assertThat(resolveAt(items, wHoNew, kst(day, 10, 15))).isEqualTo("박반장|WATCH_DEVICE");
        assertThat(resolveAt(items, wHoOld, kst(day, 10, 15))).isEqualTo("박반장|WATCH_DEVICE");
        assertThat(resolveAt(items, wHoNew, kst(day, 10, 45))).isNull();

        // 분할 자식은 부모 이름을 물려받지 않는다
        assertThat(resolveAt(items, wSplitChild, kst(day, 10, 15))).isNull();
    }

    private JsonNode fetchItems(LocalDate date) throws Exception {
        HttpResponse<String> resp = get("/api/people/display-names?date=" + date, viewerJwt);
        assertThat(resp.statusCode()).isEqualTo(200);
        return mapper.readTree(resp.body()).path("items");
    }

    private static String resolveAt(JsonNode items, String worldId, Instant at) {
        long ms = at.toEpochMilli();
        for (JsonNode item : items) {
            if (worldId.equals(item.path("world_id").asString(""))
                    && item.path("from_ms").asLong() <= ms
                    && ms < item.path("to_ms").asLong()) {
                return item.path("display_name").asString() + "|" + item.path("name_source").asString();
            }
        }
        return null;
    }

    private void insertWatchName(String deviceId, String action, String name, Instant at) {
        jdbc.update("""
                INSERT INTO watch_display_name_history
                    (site_id, device_id, display_name, action, changed_by_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """, siteId, deviceId, name, action, adminId, utc(at));
    }

    private void insertWorldName(LocalDate day, String worldId, String action, String name, String sourceWorldId,
            Instant at) {
        jdbc.update("""
                INSERT INTO person_world_name_history
                    (site_id, site_date, world_id, display_name, action, source_world_id, changed_by_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, siteId, day, worldId, name, action, sourceWorldId, adminId, utc(at));
    }

    private void insertMergeLog(LocalDate day, String oldWorldId, String survivorWorldId, Instant at) {
        jdbc.update("""
                INSERT INTO person_world_merge_log (site_id, site_date, old_world_id, survivor_world_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """, siteId, day, oldWorldId, survivorWorldId, utc(at));
    }

    private void insertBinding(String deviceId, String worldId, Instant boundAt, Instant unboundAt, String unbindReason,
            String handoffFrom, Instant handoffConfirmedAt) {
        jdbc.update("""
                INSERT INTO watch_binding
                    (device_id, world_id, method, score, bound_by_user_id, bound_at, unbound_at, unbind_reason,
                     handoff_from, handoff_confirmed_at)
                VALUES (?, ?, 'MANUAL', 1, ?, ?, ?, ?, ?, ?)
                """, deviceId, worldId, adminId, utc(boundAt),
                unboundAt == null ? null : utc(unboundAt), unbindReason, handoffFrom,
                handoffConfirmedAt == null ? null : utc(handoffConfirmedAt));
    }

    private static Instant kst(LocalDate day, int hour, int minute) {
        return ZonedDateTime.of(day.getYear(), day.getMonthValue(), day.getDayOfMonth(),
                hour, minute, 0, 0, PersonDisplayNames.SITE_ZONE).toInstant();
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }

    private String registerDevice(String id) {
        machineTokens.registerDevice(id, "WATCH");
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
}
