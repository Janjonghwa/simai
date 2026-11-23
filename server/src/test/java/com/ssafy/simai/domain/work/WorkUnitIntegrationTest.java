package com.ssafy.simai.domain.work;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** TBM 작업 저장·목록 API(GET·POST·PUT·DELETE /api/work-units) 통합 시험 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class WorkUnitIntegrationTest {

    private static final String PASSWORD = "correct-horse";
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @Autowired
    MockMvc mvc;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    AppUserRepository users;

    @Autowired
    PasswordEncoder passwordEncoder;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String zoneId = "z-work-" + suffix;
    private final String otherZoneId = "z-work2-" + suffix;
    private final String gatewayId = "tier0-work-" + suffix;
    // 구역 밖 작업은 현장 전체 이벤트를 세므로, 다른 시험이 남긴 최근 이벤트와 겹치지 않게 40일 전 근처의 임의 분에 둔다
    private final Instant start = Instant.now().minus(Duration.ofDays(40))
            .plus(Duration.ofMinutes(ThreadLocalRandom.current().nextInt(24 * 60))).truncatedTo(ChronoUnit.MINUTES);
    private final Instant end = start.plus(Duration.ofHours(2));

    @BeforeEach
    void setUp() {
        String site = jdbc.queryForObject("SELECT site_id FROM site", String.class);
        zone(site, zoneId, "개구부 구역");
        zone(site, otherZoneId, "단상 구역");
        // 구간 안: 이 구역 CRITICAL 1·HIGH 2(끝 시각 포함)·CAUTION 1·INFO 1, 다른 구역 HIGH 1. 구간 밖: 이 구역 CRITICAL 1. REPLAY 는 세지 않는다
        event("CRITICAL", zoneId, start.plusSeconds(60), "LIVE");
        event("HIGH", zoneId, start.plusSeconds(120), "LIVE");
        event("HIGH", zoneId, end, "LIVE");
        event("HIGH", otherZoneId, start.plusSeconds(180), "LIVE");
        // 알림 4단계 뒤 주의 경보는 CAUTION 이다: HIGH 와 같은 주의 칸에 센다. INFO 는 세지 않는다(S15P31A101-846)
        event("CAUTION", zoneId, start.plusSeconds(300), "LIVE", "R5");
        event("INFO", zoneId, start.plusSeconds(360), "LIVE", "R9");
        event("CRITICAL", zoneId, end.plusSeconds(60), "LIVE");
        event("CRITICAL", zoneId, start.plusSeconds(240), "REPLAY");
    }

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM work_unit WHERE created_by IN (SELECT user_id FROM app_user WHERE login_id LIKE ?)", "%-" + suffix);
        jdbc.update("DELETE FROM risk_event WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM zone WHERE zone_id IN (?, ?)", zoneId, otherZoneId);
        jdbc.update("DELETE FROM refresh_token WHERE user_id IN (SELECT user_id FROM app_user WHERE login_id LIKE ?)", "%-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id LIKE ?", "%-" + suffix);
    }

    @Test
    void 등록한_작업이_목록에_나오고_구간_안_이벤트_수를_구역별로_세며_고치고_지울_수_있다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        String viewer = token("viewer", Role.VIEWER);

        String created = mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body("2026-11-02", "  양중 작업  ", start, end, zoneId, null, 3, "B조")))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.name").value("양중 작업"))
                .andExpect(jsonPath("$.date").value("2026-11-02"))
                .andExpect(jsonPath("$.from_ms").value(start.toEpochMilli()))
                .andExpect(jsonPath("$.to_ms").value(end.toEpochMilli()))
                .andExpect(jsonPath("$.zone_id").value(zoneId))
                // 장소를 비우면 구역 이름을 보인다
                .andExpect(jsonPath("$.place").value("개구부 구역"))
                .andExpect(jsonPath("$.planned_people").value(3))
                .andExpect(jsonPath("$.lead").value("B조"))
                .andExpect(jsonPath("$.detail").doesNotExist())
                .andExpect(jsonPath("$.event_counts.CRITICAL").value(1))
                // 주의 칸은 HIGH 2 + CAUTION 1, INFO 는 빠진다(S15P31A101-846)
                .andExpect(jsonPath("$.event_counts.HIGH").value(3))
                .andExpect(jsonPath("$.event_counts.CAUTION").doesNotExist())
                // 같은 사람·같은 위험의 경보는 관제 카드 기준으로 한 사건이다(등급은 든 경보 중 가장 높은 위험, S15P31A101-751).
                // R5 CAUTION 은 따로 주의 1사건, R9 INFO 는 세지 않는다
                .andExpect(jsonPath("$.incident_counts.CRITICAL").value(1))
                .andExpect(jsonPath("$.incident_counts.HIGH").value(1))
                .andReturn().getResponse().getContentAsString();
        String workId = JsonPath.read(created, "$.work_id");

        // 구역 밖 작업은 현장 전체를 센다
        mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body("2026-11-02", "자재 정리", start, end, null, "야적장", 0, null)))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.zone_id").value((Object) null))
                .andExpect(jsonPath("$.place").value("야적장"))
                .andExpect(jsonPath("$.lead").value((Object) null))
                .andExpect(jsonPath("$.event_counts.CRITICAL").value(1))
                .andExpect(jsonPath("$.event_counts.HIGH").value(4))
                .andExpect(jsonPath("$.incident_counts.CRITICAL").value(1))
                .andExpect(jsonPath("$.incident_counts.HIGH").value(2));

        // 열람 계정도 목록을 본다
        String list = mvc.perform(get("/api/work-units").param("days", "90").header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();
        List<String> listed = JsonPath.read(list, "$.works[*].work_id");
        assertThat(listed).contains(workId);

        // 전체 바꾸기: 구역·시간을 바꾸면 이벤트 수도 바뀐다
        mvc.perform(put("/api/work-units/{id}", workId).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body("2026-11-02", "양중 작업(오전)", start, start.plusSeconds(90), otherZoneId, "북측 계단", 4, "A조")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.name").value("양중 작업(오전)"))
                .andExpect(jsonPath("$.zone_id").value(otherZoneId))
                .andExpect(jsonPath("$.place").value("북측 계단"))
                .andExpect(jsonPath("$.event_counts.CRITICAL").value(0))
                .andExpect(jsonPath("$.event_counts.HIGH").value(0));
        mvc.perform(get("/api/work-units/{id}", workId).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.lead").value("A조"));

        mvc.perform(delete("/api/work-units/{id}", workId).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isNoContent());
        mvc.perform(get("/api/work-units/{id}", workId).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("WORK_UNIT_NOT_FOUND"));
        mvc.perform(delete("/api/work-units/{id}", workId).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isNotFound());
    }

    @Test
    void 목록은_끝난_지_days_일이_안_된_작업만_준다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        Instant oldStart = Instant.now().minus(Duration.ofDays(3));
        String old = mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body("2026-10-30", "지난 작업", oldStart, oldStart.plusSeconds(3600), null, "현장", 1, null)))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
        String oldId = JsonPath.read(old, "$.work_id");

        String recent = mvc.perform(get("/api/work-units").param("days", "2").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        List<String> recentIds = JsonPath.read(recent, "$.works[*].work_id");
        assertThat(recentIds).doesNotContain(oldId);
        String week = mvc.perform(get("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        List<String> weekIds = JsonPath.read(week, "$.works[*].work_id");
        assertThat(weekIds).contains(oldId);

        mvc.perform(get("/api/work-units").param("days", "0").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_WORK_DAYS"));
        mvc.perform(get("/api/work-units").param("days", "91").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isBadRequest());
    }

    @Test
    void 권한과_입력_검증_반례를_거부한다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        String foreman = token("foreman", Role.FOREMAN);
        String ok = body("2026-11-02", "작업", start, end, zoneId, null, 2, null);

        mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, foreman)
                .contentType(MediaType.APPLICATION_JSON).content(ok)).andExpect(status().isForbidden());
        mvc.perform(post("/api/work-units").contentType(MediaType.APPLICATION_JSON).content(ok))
                .andExpect(status().isUnauthorized());
        mvc.perform(get("/api/work-units")).andExpect(status().isUnauthorized());

        String[] invalid = {
                body("2026-11-02", "   ", start, end, null, null, 2, null),
                body("2026-11-02", "가".repeat(101), start, end, null, null, 2, null),
                body("2026/11/02", "작업", start, end, null, null, 2, null),
                body("2026-02-30", "작업", start, end, null, null, 2, null),
                body("2026-11-02", "작업", end, start, null, null, 2, null),
                body("2026-11-02", "작업", start, start, null, null, 2, null),
                body("2026-11-02", "작업", start, start.plus(Duration.ofHours(25)), null, null, 2, null),
                body("2026-11-02", "작업", start, end, null, null, -1, null),
                body("2026-11-02", "작업", start, end, null, null, 101, null),
                body("2026-11-02", "작업", start, end, null, null, null, null),
                body("2026-11-02", "작업" + (char) 0, start, end, null, null, 2, null),
                body("2026-11-02", "작업", start, end, null, "가".repeat(101), 2, null),
                body("2026-11-02", "작업", start, end, null, null, 2, "가".repeat(51)),
                body("2026-11-02", "작업", Instant.ofEpochMilli(253_402_300_799_999L), Instant.ofEpochMilli(253_402_300_800_000L),
                        null, null, 2, null),
                "{}"
        };
        for (String b : invalid) {
            mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON).content(b))
                    .andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.code").value("INVALID_WORK_UNIT"));
        }
        mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body("2026-11-02", "작업", start, end, "z-none-" + suffix, null, 2, null)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("UNKNOWN_WORK_ZONE"));
        // 100자·24시간·100명 경계는 받는다
        mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body("2026-11-02", "가".repeat(100), start, start.plus(Duration.ofHours(24)), null, null, 100, null)))
                .andExpect(status().isCreated());
        mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_WORK_UNIT"));
        String created = mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(ok))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
        mvc.perform(put("/api/work-units/{id}", (String) JsonPath.read(created, "$.work_id"))
                        .header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                        .content(body("2026-11-02", "작업", start, end, "z-none-" + suffix, null, 2, null)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("UNKNOWN_WORK_ZONE"));
        mvc.perform(put("/api/work-units/{id}", 999_999_999L).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(ok))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("WORK_UNIT_NOT_FOUND"));
    }

    @Test
    void 공종과_레이아웃을_작업에_연결한다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        String site = jdbc.queryForObject("SELECT site_id FROM site", String.class);
        Long layout = jdbc.queryForObject("""
                INSERT INTO layout_plan (site_id, name, items, revision, source, created_at, updated_at)
                VALUES (?, '시험', '[]'::jsonb, 1, 'PLANNED', now(), now()) RETURNING layout_id
                """, Long.class, site);
        try {
            ObjectNode request = (ObjectNode) MAPPER.readTree(body("2026-11-02", "작업", start, end, zoneId, null, 2, null));
            request.put("layout_id", layout);
            request.put("work_type", "OPENING_WORK");
            mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON).content(MAPPER.writeValueAsString(request)))
                    .andExpect(status().isCreated())
                    .andExpect(jsonPath("$.layout_id").value(layout))
                    .andExpect(jsonPath("$.work_type").value("OPENING_WORK"));
            mvc.perform(get("/api/work-types").header(HttpHeaders.AUTHORIZATION, admin))
                    .andExpect(status().isOk()).andExpect(jsonPath("$[1].code").value("OPENING_WORK"));
            request.put("work_type", "UNKNOWN");
            mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON).content(MAPPER.writeValueAsString(request)))
                    .andExpect(status().isBadRequest());
        } finally {
            jdbc.update("DELETE FROM work_unit WHERE layout_id = ?", layout);
            jdbc.update("DELETE FROM layout_plan WHERE layout_id = ?", layout);
        }
    }

    // ---- 도우미

    private static String body(String date, String name, Instant from, Instant to, String zone, String place,
            Integer people, String lead) {
        ObjectNode b = MAPPER.createObjectNode();
        b.put("date", date);
        b.put("name", name);
        b.put("from_ms", from.toEpochMilli());
        b.put("to_ms", to.toEpochMilli());
        if (zone != null) {
            b.put("zone_id", zone);
        }
        if (place != null) {
            b.put("place", place);
        }
        if (people != null) {
            b.put("planned_people", people);
        }
        if (lead != null) {
            b.put("lead", lead);
        }
        return MAPPER.writeValueAsString(b);
    }

    private void zone(String site, String id, String name) {
        jdbc.update("""
                INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from)
                VALUES (?, 1, ?, ?, 'OPENING', 'GROUND_FOOTPRINT', ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 0))', 0), 'HIGH', now())
                """, id, site, name);
    }

    private void event(String severity, String zone, Instant raisedAt, String source) {
        event(severity, zone, raisedAt, source, "R1");
    }

    private void event(String severity, String zone, Instant raisedAt, String source, String rule) {
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity,
                    confidence, needs_check, world_ids, zone_id, metrics, resume_count, event_sequence, raised_at, updated_at,
                    delivery, retention_hold)
                VALUES (?, ?, ?, ?, 1, 'RAISED', 'VERIFIED', ?, 'HIGH', false, '{w-1}', ?, '{}', 0, 1, ?, ?, 'REALTIME', false)
                """, UUID.randomUUID(), source, gatewayId, rule, severity, zone, Timestamp.from(raisedAt), Timestamp.from(raisedAt));
    }

    private String token(String name, Role role) throws Exception {
        String loginId = name + "-" + suffix;
        if (users.findByLoginId(loginId).isEmpty()) {
            users.saveAndFlush(new AppUser(loginId, passwordEncoder.encode(PASSWORD), role, null, Instant.now()));
        }
        String body = mvc.perform(post("/api/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + loginId + "\",\"password\":\"" + PASSWORD + "\"}"))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        return "Bearer " + JsonPath.read(body, "$.accessToken");
    }
}
