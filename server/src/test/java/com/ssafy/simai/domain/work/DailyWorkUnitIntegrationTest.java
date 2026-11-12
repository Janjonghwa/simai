package com.ssafy.simai.domain.work;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** 날짜 작업 만들기·달력 목록·진행 칩·오늘만 되는 검사(S15P31A101-610) */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class DailyWorkUnitIntegrationTest {

    private static final ZoneId SEOUL = ZoneId.of("Asia/Seoul");

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired JwtEncoder encoder;
    @Autowired ObjectMapper mapper;
    @Autowired WorkUnitRepository works;

    long adminId;
    String admin;
    String viewer;
    String site;
    final LocalDate today = LocalDate.now(SEOUL);

    @BeforeEach
    void setup() {
        String tag = UUID.randomUUID().toString().substring(0, 8);
        adminId = user("admin-" + tag, "ADMIN");
        admin = token(adminId, "ADMIN");
        viewer = token(user("viewer-" + tag, "VIEWER"), "VIEWER");
        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        // 다른 시험이 남긴 날짜 작업이 하루 하나 색인에 걸리지 않게 비운다(트랜잭션이라 끝나면 되돌려진다)
        jdbc.update("DELETE FROM report_signature WHERE report_id IN (SELECT r.report_id FROM report r JOIN work_unit w ON w.work_id = r.work_id WHERE w.daily)");
        jdbc.update("DELETE FROM report WHERE work_id IN (SELECT work_id FROM work_unit WHERE daily)");
        jdbc.update("DELETE FROM work_unit WHERE daily AND NOT EXISTS (SELECT 1 FROM setup_session s WHERE s.work_unit_id = work_unit.work_id)");
    }

    @Test
    void 날짜_작업은_하루에_하나이고_빈_보고서와_함께_만들어진다() throws Exception {
        JsonNode first = body(daily(admin, today).andExpect(status().isOk()));
        assertThat(first.path("daily").asBoolean()).isTrue();
        assertThat(first.path("date").asString()).isEqualTo(today.toString());
        assertThat(first.path("name").asString()).isEmpty();
        assertThat(first.path("status").asString()).isEqualTo("PLANNED");
        assertThat(first.path("progress").asString()).isEqualTo("REPORT");
        assertThat(first.path("layout_id").isNull()).isTrue();
        // 계획 시간은 그날 0시~24시(Asia/Seoul)
        assertThat(first.path("from_ms").asLong()).isEqualTo(today.atStartOfDay(SEOUL).toInstant().toEpochMilli());
        assertThat(first.path("to_ms").asLong()).isEqualTo(today.plusDays(1).atStartOfDay(SEOUL).toInstant().toEpochMilli());
        long reportId = first.path("tbm_report").path("report_id").asLong();
        assertThat(reportId).isPositive();
        assertThat(first.path("tbm_report").path("draft_status").isNull()).isTrue();
        assertThat(first.path("tbm_report").path("submitted_at").isNull()).isTrue();

        JsonNode again = body(daily(admin, today).andExpect(status().isOk()));
        assertThat(again.path("work_id").asString()).isEqualTo(first.path("work_id").asString());
        assertThat(again.path("tbm_report").path("report_id").asLong()).isEqualTo(reportId);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM work_unit WHERE daily AND work_date = ?", Integer.class, today))
                .isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report WHERE work_id = ?", Integer.class,
                Long.parseLong(first.path("work_id").asString()))).isEqualTo(1);
    }

    @Test
    void 미래는_만들고_지난_날짜는_새로_만들지_않는다() throws Exception {
        daily(admin, today.plusDays(10)).andExpect(status().isOk())
                .andExpect(jsonPath("$.daily").value(true));
        daily(admin, today.minusDays(1)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("WORK_DATE_PAST"));

        // 이미 있던 지난 날짜 작업은 돌려준다(다시 보기)
        LocalDate past = today.minusDays(5);
        jdbc.update("""
                INSERT INTO work_unit(site_id, work_date, starts_at, ends_at, planned_people, daily, created_by, created_at, updated_at)
                VALUES (?, ?, now() - interval '5 days', now() - interval '4 days', 0, true, ?, now(), now())
                """, site, past, adminId);
        daily(admin, past).andExpect(status().isOk()).andExpect(jsonPath("$.date").value(past.toString()))
                // 지난 날짜는 다시 보기만 한다(보고서를 새로 만들지 않는다)
                .andExpect(jsonPath("$.tbm_report").doesNotExist());

        daily(viewer, today).andExpect(status().isForbidden());
        mvc.perform(put("/api/work-units/daily/{d}", "2026-13-01").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isBadRequest());
    }

    @Test
    void 달력은_작업_날짜_구간으로_받고_예전_작업도_나온다() throws Exception {
        LocalDate from = today.plusDays(30);
        LocalDate to = from.plusDays(29);
        daily(admin, from);
        daily(admin, to);
        daily(admin, to.plusDays(1));
        jdbc.update("""
                INSERT INTO work_unit(site_id, work_date, name, starts_at, ends_at, planned_people, created_by, created_at, updated_at)
                VALUES (?, ?, '예전 작업', now(), now() + interval '1 hour', 3, ?, now(), now())
                """, site, from.plusDays(3), adminId);

        JsonNode list = body(mvc.perform(get("/api/work-units").param("from", from.toString()).param("to", to.toString())
                .header(HttpHeaders.AUTHORIZATION, viewer)).andExpect(status().isOk())).path("works");
        assertThat(list.size()).isEqualTo(3);
        assertThat(list.get(0).path("date").asString()).isEqualTo(from.toString());
        assertThat(list.get(1).path("name").asString()).isEqualTo("예전 작업");
        assertThat(list.get(1).path("daily").asBoolean()).isFalse();
        assertThat(list.get(1).path("tbm_report").isNull()).isTrue();
        assertThat(list.get(2).path("date").asString()).isEqualTo(to.toString());

        for (String[] bad : new String[][] {{from.toString(), null}, {to.toString(), from.toString()},
                {from.toString(), from.plusDays(92).toString()}, {"2026-1-1", "2026-01-31"}}) {
            var req = get("/api/work-units").header(HttpHeaders.AUTHORIZATION, viewer).param("from", bad[0]);
            if (bad[1] != null) req = req.param("to", bad[1]);
            mvc.perform(req).andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_WORK_RANGE"));
        }
        // 날짜를 주지 않으면 예전 days 목록 그대로
        mvc.perform(get("/api/work-units").header(HttpHeaders.AUTHORIZATION, viewer)).andExpect(status().isOk());
    }

    @Test
    void 작업내용을_고르면_작업_이름이_첫_항목_외_N건이_된다() throws Exception {
        JsonNode work = body(daily(admin, today));
        long reportId = work.path("tbm_report").path("report_id").asLong();
        ObjectNode form = (ObjectNode) body(mvc.perform(get("/api/reports/tbm/{id}", reportId)
                .header(HttpHeaders.AUTHORIZATION, admin))).path("form").deepCopy();
        ArrayNode tasks = ((ObjectNode) form.path("info")).putArray("tasks");
        tasks.addObject().put("code", "SLAB_OPENING_COVER");
        tasks.addObject().put("text", "전기 배선 정리");
        tasks.addObject().put("code", "REBAR");
        putForm(reportId, form, 0);
        assertThat(one(work).path("name").asString()).isEqualTo("슬래브 개구부 덮개 설치 외 2건");

        ((ObjectNode) form.path("info")).putArray("tasks").addObject().put("text", "전기 배선 정리");
        putForm(reportId, form, 1);
        assertThat(one(work).path("name").asString()).isEqualTo("전기 배선 정리");

        ((ObjectNode) form.path("info")).putArray("tasks");
        putForm(reportId, form, 2);
        assertThat(one(work).path("name").asString()).isEmpty();
    }

    @Test
    void 긴_작업내용으로_만든_이름도_100자를_넘지_않는다() throws Exception {
        JsonNode work = body(daily(admin, today));
        long reportId = work.path("tbm_report").path("report_id").asLong();
        ObjectNode form = (ObjectNode) body(mvc.perform(get("/api/reports/tbm/{id}", reportId)
                .header(HttpHeaders.AUTHORIZATION, admin))).path("form").deepCopy();
        ArrayNode tasks = ((ObjectNode) form.path("info")).putArray("tasks");
        tasks.addObject().put("text", "가".repeat(100));
        tasks.addObject().put("code", "REBAR");
        putForm(reportId, form, 0);
        String name = one(work).path("name").asString();
        assertThat(name.codePointCount(0, name.length())).isEqualTo(100);
        assertThat(name).endsWith("… 외 1건");
    }

    @Test
    void 날짜_작업은_작업_수정_삭제_API_로_바꾸지_않는다() throws Exception {
        JsonNode work = body(daily(admin, today));
        String id = work.path("work_id").asString();
        mvc.perform(put("/api/work-units/{id}", id).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"date\":\"" + today + "\",\"name\":\"x\",\"from_ms\":1,\"to_ms\":2,\"planned_people\":0}"))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
        mvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete("/api/work-units/{id}", id)
                        .header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
    }

    @Test
    void 날짜_작업_시작은_오늘이고_세팅_완료와_서명이_있어야_한다() throws Exception {
        long future = Long.parseLong(body(daily(admin, today.plusDays(2))).path("work_id").asString());
        assertThatThrownBy(() -> works.startSession(site, future, Instant.now()))
                .isInstanceOf(BusinessException.class).hasMessage("WORK_NOT_TODAY");
        assertThatThrownBy(() -> works.requireToday(site, future, Instant.now()))
                .isInstanceOf(BusinessException.class);

        JsonNode todayWork = body(daily(admin, today));
        long workId = Long.parseLong(todayWork.path("work_id").asString());
        works.requireToday(site, workId, Instant.now());
        // 세팅 완료가 서명보다 앞이다(세팅 → 작업 전 확인 → 시작, -642)
        assertThatThrownBy(() -> works.startSession(site, workId, Instant.now()))
                .isInstanceOf(BusinessException.class).hasMessage("WORK_SETUP_NOT_COMPLETED");
        jdbc.update("UPDATE work_unit SET setup_completed_at = now() WHERE work_id = ?", workId);
        assertThatThrownBy(() -> works.startSession(site, workId, Instant.now()))
                .isInstanceOf(BusinessException.class).hasMessage("WORK_NO_SIGNATURE");

        jdbc.update("""
                INSERT INTO report_signature(report_id, report_version, signer_name, signature_image_key, image_sha256,
                    report_content_sha256, legal_notice_ack, recorded_by, signed_at)
                VALUES (?, 1, '참석자', ?, 'a', 'b', true, ?, now())
                """, todayWork.path("tbm_report").path("report_id").asLong(), "sig-" + UUID.randomUUID(), adminId);
        // 다른 작업이 진행 중이면 시작이 막히므로 이 시험 안에서만 끝낸다
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = GREATEST(now(), actual_started_at) WHERE status = 'IN_PROGRESS'");
        ObjectNode started = works.startSession(site, workId, Instant.now());
        assertThat(started.path("status").asString()).isEqualTo("IN_PROGRESS");
        assertThat(started.path("progress").asString()).isEqualTo("IN_PROGRESS");

        // 예전 작업은 날짜·서명 검사를 하지 않는다
        Long legacy = jdbc.queryForObject("""
                INSERT INTO work_unit(site_id, work_date, name, starts_at, ends_at, planned_people, created_by, created_at, updated_at)
                VALUES (?, ?, '예전', now(), now() + interval '1 hour', 0, ?, now(), now()) RETURNING work_id
                """, Long.class, site, today.minusDays(3), adminId);
        works.requireToday(site, legacy, Instant.now());
    }

    @Test
    void 세팅을_완료하면_진행_칩이_SETUP_DONE_이다() throws Exception {
        JsonNode work = body(daily(admin, today));
        jdbc.update("UPDATE work_unit SET setup_completed_at = now() WHERE work_id = ?", Long.parseLong(work.path("work_id").asString()));
        JsonNode after = one(work);
        assertThat(after.path("progress").asString()).isEqualTo("SETUP_DONE");
        assertThat(after.path("setup_completed_at").isNumber()).isTrue();
    }

    @Test
    void 진행_칩은_뒤_단계부터_정한다() {
        assertThat(WorkUnitRepository.progress("PLANNED", false, false, false)).isEqualTo("REPORT");
        assertThat(WorkUnitRepository.progress("PLANNED", false, false, true)).isEqualTo("SETUP");
        assertThat(WorkUnitRepository.progress("PLANNED", false, true, true)).isEqualTo("SETUP_DONE");
        assertThat(WorkUnitRepository.progress("IN_PROGRESS", false, true, true)).isEqualTo("IN_PROGRESS");
        assertThat(WorkUnitRepository.progress("DONE", false, true, true)).isEqualTo("ENDED");
        assertThat(WorkUnitRepository.progress("DONE", true, true, true)).isEqualTo("SUBMITTED");
    }

    private JsonNode one(JsonNode work) throws Exception {
        return body(mvc.perform(get("/api/work-units/{id}", work.path("work_id").asString())
                .header(HttpHeaders.AUTHORIZATION, viewer)).andExpect(status().isOk()));
    }

    private void putForm(long reportId, JsonNode form, int revision) throws Exception {
        ObjectNode b = mapper.createObjectNode();
        b.set("form", form);
        b.put("form_revision", revision);
        mvc.perform(put("/api/reports/tbm/{id}/form", reportId).header(HttpHeaders.AUTHORIZATION, admin)
                .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(b))).andExpect(status().isOk());
    }

    private ResultActions daily(String token, LocalDate date) throws Exception {
        return mvc.perform(put("/api/work-units/daily/{d}", date.toString()).header(HttpHeaders.AUTHORIZATION, token));
    }

    private long user(String login, String role) {
        return jdbc.queryForObject("INSERT INTO app_user(login_id,password_hash,role,created_at) VALUES (?, 'x', ?, now()) RETURNING user_id",
                Long.class, login, role);
    }

    private String token(long id, String role) {
        Instant now = Instant.now();
        var claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(id)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return "Bearer " + encoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private JsonNode body(ResultActions actions) throws Exception {
        return mapper.readTree(actions.andReturn().getResponse().getContentAsString());
    }
}
