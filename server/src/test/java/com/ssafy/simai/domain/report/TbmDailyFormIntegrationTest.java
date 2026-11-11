package com.ssafy.simai.domain.report;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.user.service.UserService;
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

/** 날짜 작업 TBM 보고서: 빈 보고서·양식 칸 저장·리더 계정·작업내용 항목(S15P31A101-609) */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class TbmDailyFormIntegrationTest {

    private static final ZoneId SEOUL = ZoneId.of("Asia/Seoul");

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired JwtEncoder encoder;
    @Autowired ObjectMapper mapper;
    @Autowired TbmReportService reports;
    @Autowired UserService userService;

    String tag;
    long adminId;
    String admin;
    String viewer;

    @BeforeEach
    void setup() {
        tag = UUID.randomUUID().toString().substring(0, 8);
        adminId = user("admin1-" + tag, "ADMIN");
        admin = token(adminId, "ADMIN");
        viewer = token(user("viewer-" + tag, "VIEWER"), "VIEWER");
        userService.setProfile("admin1-" + tag, "곽나혁", "시마이건설", "안전관리자");
    }

    @Test
    void 오늘_날짜_작업의_빈_보고서는_PENDING_빈_양식과_리더_계정을_가진다() throws Exception {
        long workId = work(LocalDate.now(SEOUL));
        long before = Instant.now().toEpochMilli();
        long reportId = reports.createDailyBlank(workId, adminId);

        JsonNode r = body(mvc.perform(get("/api/reports/tbm/{id}", reportId).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isOk()));
        assertThat(r.path("status").asString()).isEqualTo("PENDING");
        assertThat(r.path("draft_status").isNull()).isTrue();
        assertThat(r.path("layout_id").isNull()).isTrue();
        assertThat(r.path("form_revision").asInt()).isZero();
        JsonNode form = r.path("form");
        assertThat(form.path("info").path("date_time_ms").asLong()).isBetween(before - 1000, Instant.now().toEpochMilli() + 1000);
        assertThat(form.path("info").path("same_day").asBoolean()).isTrue();
        assertThat(form.path("info").path("tasks").size()).isZero();
        assertThat(form.path("info").path("risk_assessed").isNull()).isTrue();
        assertThat(form.path("risks").size()).isEqualTo(3);
        assertThat(form.path("checks").size()).isEqualTo(5);
        assertThat(form.path("checks").get(0).path("code").asString()).isEqualTo("PPE");
        assertThat(form.path("checks").get(0).path("label").asString()).isEqualTo("안전모 등 보호구 착용 상태 확인");
        assertThat(form.path("checks").get(4).path("code").asString()).isEqualTo("HEALTH");
        assertThat(form.path("review").path("ending").asString()).isEmpty();
        // 리더는 계정에서 온다(이름 글자를 보고서에 저장하지 않는다).
        assertThat(r.path("leader").path("user_id").asLong()).isEqualTo(adminId);
        assertThat(r.path("leader").path("name").asString()).isEqualTo("곽나혁");
        assertThat(r.path("leader").path("org").asString()).isEqualTo("시마이건설");
        assertThat(r.path("leader").path("title").asString()).isEqualTo("안전관리자");

        // 여러 번 불러도 보고서는 하나다.
        assertThat(reports.createDailyBlank(workId, adminId)).isEqualTo(reportId);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report WHERE work_id=?", Integer.class, workId)).isEqualTo(1);
    }

    @Test
    void 미래_날짜의_빈_보고서_일시는_그날_07시30분이다() {
        LocalDate date = LocalDate.now(SEOUL).plusDays(3);
        long reportId = reports.createDailyBlank(work(date), adminId);
        long expected = date.atTime(7, 30).atZone(SEOUL).toInstant().toEpochMilli();
        assertThat(reports.getOne(reportId).path("form").path("info").path("date_time_ms").asLong()).isEqualTo(expected);
    }

    @Test
    void 양식을_통째로_저장하면_버전은_그대로이고_revision_만_오른다() throws Exception {
        long workId = work(LocalDate.now(SEOUL));
        long reportId = reports.createDailyBlank(workId, adminId);
        ObjectNode form = (ObjectNode) reports.getOne(reportId).path("form").deepCopy();
        ObjectNode info = (ObjectNode) form.path("info");
        info.put("task_name", "개구부 덮개 설치 외 1건");
        info.put("place", "   ");
        ArrayNode tasks = info.putArray("tasks");
        tasks.addObject().put("code", "SLAB_OPENING_COVER");
        tasks.addObject().put("text", "  전기 배선 정리 ");
        info.put("risk_assessed", true);
        ((ObjectNode) form.path("risks").get(0)).put("risk", "개구부 추락").put("measure", "덮개 고정");
        ObjectNode check = (ObjectNode) form.path("checks").get(2);
        check.put("yes", false).put("action", "난간 보강 후 작업").put("label", "바꾼 문구");
        form.put("unknown", "버림");

        JsonNode saved = body(putForm(admin, reportId, form, 0).andExpect(status().isOk()));
        assertThat(saved.path("form_revision").asInt()).isEqualTo(1);
        assertThat(saved.path("report_version").asInt()).isEqualTo(1);
        assertThat(saved.path("status").asString()).isEqualTo("PENDING");
        JsonNode f = saved.path("form");
        assertThat(f.path("info").path("tasks").get(1).path("text").asString()).isEqualTo("전기 배선 정리");
        assertThat(f.path("info").path("risk_assessed").asBoolean()).isTrue();
        // 공백만 있는 칸은 빈 칸으로 저장한다(AI 초안이 빈 칸으로 보고 채울 수 있게).
        assertThat(f.path("info").path("place").asString()).isEmpty();
        assertThat(f.path("risks").get(0).path("measure").asString()).isEqualTo("덮개 고정");
        assertThat(f.path("checks").get(2).path("yes").asBoolean()).isFalse();
        // 점검 문구는 서버가 정한다.
        assertThat(f.path("checks").get(2).path("label").asString()).isEqualTo("개구부 덮개·난간 설치 확인");
        assertThat(f.has("unknown")).isFalse();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report WHERE work_id=?", Integer.class, workId)).isEqualTo(1);

        // 마지막으로 읽은 revision 이 아니면 409
        putForm(admin, reportId, form, 0).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REPORT_FORM_CONFLICT"));
        putForm(admin, reportId, form, 1).andExpect(status().isOk()).andExpect(jsonPath("$.form_revision").value(2));
    }

    @Test
    void 양식_모양이_틀리면_400_권한이_없으면_403() throws Exception {
        long reportId = reports.createDailyBlank(work(LocalDate.now(SEOUL)), adminId);
        JsonNode blank = reports.getOne(reportId).path("form");

        putForm(viewer, reportId, blank, 0).andExpect(status().isForbidden());

        ObjectNode twoRisks = (ObjectNode) blank.deepCopy();
        ((ArrayNode) twoRisks.path("risks")).remove(0);
        assertBad(reportId, twoRisks);

        ObjectNode badTask = (ObjectNode) blank.deepCopy();
        ((ObjectNode) badTask.path("info")).putArray("tasks").addObject().put("code", "NO_SUCH_TASK");
        assertBad(reportId, badTask);

        ObjectNode dupTask = (ObjectNode) blank.deepCopy();
        ArrayNode dup = ((ObjectNode) dupTask.path("info")).putArray("tasks");
        dup.addObject().put("code", "REBAR");
        dup.addObject().put("code", "REBAR");
        assertBad(reportId, dupTask);

        ObjectNode swapped = (ObjectNode) blank.deepCopy();
        ((ObjectNode) swapped.path("checks").get(0)).put("code", "HEALTH");
        assertBad(reportId, swapped);

        ObjectNode tooLong = (ObjectNode) blank.deepCopy();
        ((ObjectNode) tooLong.path("focus")).put("risk", "가".repeat(501));
        assertBad(reportId, tooLong);

        ObjectNode nul = (ObjectNode) blank.deepCopy();
        ((ObjectNode) nul.path("info")).put("place", "a\u0000b");
        assertBad(reportId, nul);

        ObjectNode blankText = (ObjectNode) blank.deepCopy();
        ((ObjectNode) blankText.path("info")).putArray("tasks").addObject().put("text", "   ");
        assertBad(reportId, blankText);

        ObjectNode wrongType = (ObjectNode) blank.deepCopy();
        ((ObjectNode) wrongType.path("info")).put("same_day", "예");
        assertBad(reportId, wrongType);

        mvc.perform(put("/api/reports/tbm/{id}/form", reportId).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"form\":{}}"))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_REPORT_FORM"));
        assertThat(reports.getOne(reportId).path("form_revision").asInt()).isZero();
    }

    @Test
    void 잠긴_보고서와_예전_버전은_고칠_수_없다() throws Exception {
        long workId = work(LocalDate.now(SEOUL));
        long reportId = reports.createDailyBlank(workId, adminId);
        JsonNode blank = reports.getOne(reportId).path("form");

        jdbc.update("UPDATE report SET locked_at=now(), content_sha256='x' WHERE report_id=?", reportId);
        putForm(admin, reportId, blank, 0).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REPORT_LOCKED"));

        jdbc.update("UPDATE report SET locked_at=NULL, content_sha256=NULL WHERE report_id=?", reportId);
        jdbc.update("INSERT INTO report(report_kind, work_id, report_version, status) VALUES ('TBM', ?, 2, 'SUCCEEDED')", workId);
        putForm(admin, reportId, blank, 0).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REPORT_VERSION_STALE"));

        putForm(admin, 999_999_999L, blank, 0).andExpect(status().isNotFound());
    }

    @Test
    void PENDING_은_TBM_보고서에만_허용한다() {
        assertThatThrownBy(() -> jdbc.update("""
                INSERT INTO report(report_kind, report_date, site_id, report_version, status)
                VALUES ('DAILY', CURRENT_DATE, (SELECT site_id FROM site LIMIT 1), 1, 'PENDING')
                """)).isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
    }

    @Test
    void 작업내용_항목은_7개이고_법령_태그를_가진다() throws Exception {
        JsonNode tasks = body(mvc.perform(get("/api/work-tasks").header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isOk()));
        assertThat(tasks.size()).isEqualTo(7);
        assertThat(tasks.get(0).path("code").asString()).isEqualTo("SLAB_OPENING_COVER");
        assertThat(tasks.get(0).path("label").asString()).isEqualTo("슬래브 개구부 덮개 설치");
        for (JsonNode t : tasks) {
            assertThat(t.path("rule_tags").size()).isPositive();
        }
    }

    @Test
    void 계정_소속_직책은_40자까지다() {
        assertThatThrownBy(() -> userService.setProfile("admin1-" + tag, null, "가".repeat(41), null))
                .isInstanceOf(BusinessException.class);
        // 주지 않은 칸은 그대로 둔다.
        userService.setProfile("admin1-" + tag, null, null, "현장소장");
        assertThat(jdbc.queryForObject("SELECT org || '/' || title || '/' || display_label FROM app_user WHERE user_id=?",
                String.class, adminId)).isEqualTo("시마이건설/현장소장/곽나혁");
    }

    private void assertBad(long reportId, JsonNode form) throws Exception {
        putForm(admin, reportId, form, 0).andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_REPORT_FORM"));
    }

    private ResultActions putForm(String token, long reportId, JsonNode form, int revision) throws Exception {
        ObjectNode b = mapper.createObjectNode();
        b.set("form", form);
        b.put("form_revision", revision);
        return mvc.perform(put("/api/reports/tbm/{id}/form", reportId).header(HttpHeaders.AUTHORIZATION, token)
                .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(b)));
    }

    private long work(LocalDate date) {
        return jdbc.queryForObject("""
                INSERT INTO work_unit(site_id, work_date, name, starts_at, ends_at, planned_people, created_by, created_at, updated_at)
                VALUES ((SELECT site_id FROM site LIMIT 1), ?, 'daily', now(), now() + interval '1 hour', 0, ?, now(), now())
                RETURNING work_id
                """, Long.class, date, adminId);
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
