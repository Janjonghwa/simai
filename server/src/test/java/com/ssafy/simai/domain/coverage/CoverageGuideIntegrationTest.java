package com.ssafy.simai.domain.coverage;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import com.ssafy.simai.global.security.TokenHashes;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.context.annotation.Primary;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 1단계 카메라 각도 안내 중계 통합 시험(S15P31A101-447).
 * 토큰 없음 401, 스키마 위반·귀속 불일치 400, 비노트북 기기 403, 메모리 최신 1건 갱신(DB 미적재),
 * 10 s 초과 후 STALE 표시, 관리자 조회 기반 active_demand 켜짐·꺼짐(15 s)을 검증한다.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Import(CoverageGuideIntegrationTest.ClockConfiguration.class)
class CoverageGuideIntegrationTest {

    private static final Path CONTRACTS = Path.of("..", "contracts");
    private static final String PASSWORD = "correct-horse-battery";

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper mapper;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private AppUserRepository users;

    @Autowired
    private PasswordEncoder passwords;

    @Autowired
    private CoverageGuideService coverageGuideService;

    @Autowired
    private ControllableClock clock;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String laptopId = "gw-cov-" + tag;
    private final String watchId = "watch-cov-" + tag;
    private final String laptopToken = "test-only-laptop-token-" + tag;
    private final String watchToken = "test-only-watch-token-" + tag;
    private final String adminLogin = "cov-admin-" + tag;
    private final String viewerLogin = "cov-viewer-" + tag;

    private String adminBearer;
    private String viewerBearer;

    @BeforeEach
    void setUp() throws Exception {
        clock.set(Instant.now());
        coverageGuideService.clearForTest();
        jdbc.update("INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (?, 'LAPTOP', ?, now())",
                laptopId, TokenHashes.sha256Hex(laptopToken));
        jdbc.update("INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (?, 'WATCH', ?, now())",
                watchId, TokenHashes.sha256Hex(watchToken));
        users.save(new AppUser(adminLogin, passwords.encode(PASSWORD), Role.ADMIN, "관리자", Instant.now()));
        users.save(new AppUser(viewerLogin, passwords.encode(PASSWORD), Role.VIEWER, "열람자", Instant.now()));
        adminBearer = loginBearer(adminLogin);
        viewerBearer = loginBearer(viewerLogin);
    }

    @AfterEach
    void tearDown() {
        coverageGuideService.clearForTest();
        jdbc.update("DELETE FROM device WHERE device_id IN (?, ?)", laptopId, watchId);
        jdbc.update("DELETE FROM refresh_token WHERE user_id IN (SELECT user_id FROM app_user WHERE login_id IN (?, ?))",
                adminLogin, viewerLogin);
        jdbc.update("DELETE FROM app_user WHERE login_id IN (?, ?)", adminLogin, viewerLogin);
    }

    @Test
    void 토큰이_없거나_권한이_맞지_않으면_401_또는_403_을_반환한다() throws Exception {
        ObjectNode validBody = validGuidePayload(laptopId, 1793690000000L);

        // 기기 토큰 없음 -> 401
        mockMvc.perform(post("/api/devices/coverage-guides")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.writeValueAsString(validBody)))
                .andExpect(status().isUnauthorized());

        // 잘못된 기기 토큰 -> 401
        mockMvc.perform(post("/api/devices/coverage-guides")
                        .header("Authorization", "Bearer invalid-device-token")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.writeValueAsString(validBody)))
                .andExpect(status().isUnauthorized());

        // 워치(비노트북) 기기 토큰 -> 403
        ObjectNode watchBody = validGuidePayload(watchId, 1793690000000L);
        mockMvc.perform(post("/api/devices/coverage-guides")
                        .header("Authorization", "Bearer " + watchToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.writeValueAsString(watchBody)))
                .andExpect(status().isForbidden());

        // 관리자 조회: 토큰 없음 -> 401, VIEWER -> 403
        mockMvc.perform(get("/api/cameras/coverage-guides"))
                .andExpect(status().isUnauthorized());
        mockMvc.perform(get("/api/cameras/coverage-guides")
                        .header("Authorization", viewerBearer))
                .andExpect(status().isForbidden());
    }

    @Test
    void 스키마_위반이나_현장_기기_귀속_불일치는_400_으로_거부한다() throws Exception {
        // 1. 계약 반례(사람·영상 필드가 섞인 닫힌 객체 위반) -> 400
        String invalidExample = Files.readString(
                CONTRACTS.resolve("examples/invalid/coverage_guide.extra_person_or_image_field.json"));
        ObjectNode invalidNode = (ObjectNode) mapper.readTree(invalidExample);
        invalidNode.put("source_id", laptopId);

        MvcResult extraFieldRes = mockMvc.perform(post("/api/devices/coverage-guides")
                        .header("Authorization", "Bearer " + laptopToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.writeValueAsString(invalidNode)))
                .andExpect(status().isBadRequest())
                .andReturn();
        assertThat(mapper.readTree(extraFieldRes.getResponse().getContentAsString()).path("code").asString())
                .isEqualTo("INVALID_COVERAGE_GUIDE");

        // 2. 필수 필드 누락 -> 400
        ObjectNode missingPairs = validGuidePayload(laptopId, 1793690000000L);
        missingPairs.remove("pairs");
        mockMvc.perform(post("/api/devices/coverage-guides")
                        .header("Authorization", "Bearer " + laptopToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.writeValueAsString(missingPairs)))
                .andExpect(status().isBadRequest());

        // 3. source_id 가 인증된 기기 ID 와 다름(귀속 불일치) -> 400
        ObjectNode wrongSource = validGuidePayload("other-gateway", 1793690000000L);
        MvcResult mismatchRes = mockMvc.perform(post("/api/devices/coverage-guides")
                        .header("Authorization", "Bearer " + laptopToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.writeValueAsString(wrongSource)))
                .andExpect(status().isBadRequest())
                .andReturn();
        assertThat(mapper.readTree(mismatchRes.getResponse().getContentAsString()).path("code").asString())
                .isEqualTo("COVERAGE_GUIDE_SOURCE_MISMATCH");
    }

    @Test
    void 최신_결과_1건만_메모리에_갱신하고_DB_에는_쌓지_않는다() throws Exception {
        Integer beforeCount = jdbc.queryForObject("SELECT count(*) FROM coverage_guide", Integer.class);

        // 수신 전 관리자 조회 -> EMPTY
        JsonNode emptyView = getCoverageGuides();
        assertThat(emptyView.path("status").asString()).isEqualTo("EMPTY");
        assertThat(emptyView.path("stale").asBoolean()).isFalse();
        assertThat(emptyView.path("latest").isNull()).isTrue();

        // 1차 업로드(DISCONNECTED)
        ObjectNode first = validGuidePayload(laptopId, 1793690000000L);
        first.put("connectivity", "DISCONNECTED");
        first.putArray("components")
                .add(mapper.createArrayNode().add("cam_A").add("cam_B").add("cam_C"))
                .add(mapper.createArrayNode().add("cam_D"));
        postCoverageGuide(first);

        JsonNode firstView = getCoverageGuides();
        assertThat(firstView.path("status").asString()).isEqualTo("OK");
        assertThat(firstView.path("stale").asBoolean()).isFalse();
        assertThat(firstView.at("/latest/computed_at_ms").asLong()).isEqualTo(1793690000000L);
        assertThat(firstView.at("/latest/connectivity").asString()).isEqualTo("DISCONNECTED");

        // 2차 업로드(CONNECTED, 새 시각) -> 메모리 최신본 교체
        ObjectNode second = validGuidePayload(laptopId, 1793690005000L);
        second.put("connectivity", "CONNECTED");
        second.putArray("components").addArray().add("cam_A").add("cam_B").add("cam_C").add("cam_D");
        second.putArray("guides");
        postCoverageGuide(second);

        JsonNode secondView = getCoverageGuides();
        assertThat(secondView.path("status").asString()).isEqualTo("OK");
        assertThat(secondView.path("stale").asBoolean()).isFalse();
        assertThat(secondView.at("/latest/computed_at_ms").asLong()).isEqualTo(1793690005000L);
        assertThat(secondView.at("/latest/connectivity").asString()).isEqualTo("CONNECTED");
        assertThat(secondView.at("/latest/guides")).isEmpty();

        // DB coverage_guide 테이블에는 한 행도 쌓이지 않아야 한다
        Integer afterCount = jdbc.queryForObject("SELECT count(*) FROM coverage_guide", Integer.class);
        assertThat(afterCount).isEqualTo(beforeCount);
    }

    @Test
    void 결과가_10초_넘게_묵었으면_STALE_로_표시한다() throws Exception {
        Instant t0 = clock.instant();
        ObjectNode payload = validGuidePayload(laptopId, t0.toEpochMilli());
        postCoverageGuide(payload);

        // 정확히 10 초 경과: 아직 유효(OK)
        clock.set(t0.plusSeconds(10));
        JsonNode at10s = getCoverageGuides();
        assertThat(at10s.path("status").asString()).isEqualTo("OK");
        assertThat(at10s.path("stale").asBoolean()).isFalse();
        assertThat(at10s.path("age_ms").asLong()).isEqualTo(10_000L);

        // 10 초 초과(10.001 초): STALE 표시 + 마지막 latest 본문은 유지
        clock.set(t0.plusMillis(10_001));
        JsonNode after10s = getCoverageGuides();
        assertThat(after10s.path("status").asString()).isEqualTo("STALE");
        assertThat(after10s.path("stale").asBoolean()).isTrue();
        assertThat(after10s.path("age_ms").asLong()).isEqualTo(10_001L);
        assertThat(after10s.at("/latest/source_id").asString()).isEqualTo(laptopId);
    }

    @Test
    void 관리자_조회_시각에_따라_POST_응답의_active_demand_가_켜지고_15초_뒤_꺼진다() throws Exception {
        Instant t0 = clock.instant();
        ObjectNode payload = validGuidePayload(laptopId, t0.toEpochMilli());

        // 1. 관리자 조회가 한 번도 없을 때 -> active_demand = false
        JsonNode initialPost = postCoverageGuide(payload);
        assertThat(initialPost.path("active_demand").asBoolean()).isFalse();
        assertThat(initialPost.path("demand_ttl_ms").asLong()).isEqualTo(15_000L);
        assertThat(initialPost.path("last_admin_query_at_ms").isNull()).isTrue();

        // 2. 관리자가 GET /api/cameras/coverage-guides 호출 -> 수요 켜짐
        clock.set(t0.plusSeconds(1));
        JsonNode adminView = getCoverageGuides();
        assertThat(adminView.path("active_demand").asBoolean()).isTrue();

        // 3. 관리자 조회 후 15 초 이내(예: +5 초, +15 초) 기기 POST -> active_demand = true
        clock.set(t0.plusSeconds(6));
        JsonNode duringDemand = postCoverageGuide(payload);
        assertThat(duringDemand.path("active_demand").asBoolean()).isTrue();
        assertThat(duringDemand.path("last_admin_query_at_ms").asLong()).isEqualTo(t0.plusSeconds(1).toEpochMilli());

        clock.set(t0.plusSeconds(16)); // 관리자 조회(t0+1s)로부터 정확히 15s
        JsonNode at15s = postCoverageGuide(payload);
        assertThat(at15s.path("active_demand").asBoolean()).isTrue();

        // 4. 관리자 조회 후 15 초 초과(t0+1s + 15.001s) -> active_demand = false
        clock.set(t0.plusSeconds(1).plusMillis(15_001));
        JsonNode expiredDemand = postCoverageGuide(payload);
        assertThat(expiredDemand.path("active_demand").asBoolean()).isFalse();
    }

    private ObjectNode validGuidePayload(String sourceId, long computedAtMs) throws Exception {
        String raw = Files.readString(CONTRACTS.resolve("examples/coverage_guide.json"));
        ObjectNode node = (ObjectNode) mapper.readTree(raw);
        node.put("source_id", sourceId);
        node.put("computed_at_ms", computedAtMs);
        node.put("valid_until_ms", computedAtMs + 10_000L);
        return node;
    }

    private JsonNode postCoverageGuide(ObjectNode payload) throws Exception {
        MvcResult res = mockMvc.perform(post("/api/devices/coverage-guides")
                        .header("Authorization", "Bearer " + laptopToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.writeValueAsString(payload)))
                .andExpect(status().isOk())
                .andReturn();
        return mapper.readTree(res.getResponse().getContentAsString());
    }

    private JsonNode getCoverageGuides() throws Exception {
        MvcResult res = mockMvc.perform(get("/api/cameras/coverage-guides")
                        .header("Authorization", adminBearer))
                .andExpect(status().isOk())
                .andReturn();
        return mapper.readTree(res.getResponse().getContentAsString());
    }

    private String loginBearer(String loginId) throws Exception {
        MvcResult res = mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + loginId + "\",\"password\":\"" + PASSWORD + "\"}"))
                .andExpect(status().isOk())
                .andReturn();
        String token = mapper.readTree(res.getResponse().getContentAsString()).path("accessToken").asString();
        return "Bearer " + token;
    }

    @TestConfiguration(proxyBeanMethods = false)
    static class ClockConfiguration {

        @Bean
        @Primary
        ControllableClock controllableClock() {
            return new ControllableClock();
        }
    }

    static class ControllableClock extends Clock {

        private final AtomicReference<Instant> current = new AtomicReference<>(Instant.now());

        void set(Instant instant) {
            current.set(instant);
        }

        void advance(Duration duration) {
            current.updateAndGet(prev -> prev.plus(duration));
        }

        @Override
        public ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return Clock.fixed(current.get(), zone);
        }

        @Override
        public Instant instant() {
            return current.get();
        }
    }
}
