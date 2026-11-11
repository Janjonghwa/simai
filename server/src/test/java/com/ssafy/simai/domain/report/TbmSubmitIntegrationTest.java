package com.ssafy.simai.domain.report;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.Map;
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
import tools.jackson.databind.node.ObjectNode;

/** TBM 서명 지우기·제출·제출 뒤 잠금(S15P31A101-611) */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class TbmSubmitIntegrationTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired JwtEncoder encoder;
    @Autowired ObjectMapper mapper;
    @Autowired TbmReportService reports;

    long adminId;
    String admin;
    String viewer;
    long workId;
    long reportId;

    @BeforeEach
    void setup() {
        String tag = UUID.randomUUID().toString().substring(0, 8);
        adminId = jdbc.queryForObject("INSERT INTO app_user(login_id,password_hash,role,created_at) VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id",
                Long.class, "admin-" + tag);
        jdbc.update("UPDATE app_user SET display_label = '제출 계정' WHERE user_id = ?", adminId);
        admin = token(adminId, "ADMIN");
        viewer = token(jdbc.queryForObject("INSERT INTO app_user(login_id,password_hash,role,created_at) VALUES (?, 'x', 'VIEWER', now()) RETURNING user_id",
                Long.class, "viewer-" + tag), "VIEWER");
        workId = jdbc.queryForObject("""
                INSERT INTO work_unit(site_id, work_date, name, starts_at, ends_at, planned_people, created_by, created_at, updated_at)
                VALUES ((SELECT site_id FROM site LIMIT 1), ?, 'submit', now(), now() + interval '1 hour', 0, ?, now(), now())
                RETURNING work_id
                """, Long.class, LocalDate.now(ZoneId.of("Asia/Seoul")), adminId);
        reportId = reports.createDailyBlank(workId, adminId);
    }

    @Test
    void 서명은_작업_시작_전에만_지울_수_있다() throws Exception {
        long sig = signature(reportId, 1);
        long other = signature(reportId, 1);
        mvc.perform(delete("/api/reports/tbm/{id}/signatures/{s}", reportId, sig).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isForbidden());
        mvc.perform(delete("/api/reports/tbm/{id}/signatures/{s}", reportId, sig).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isNoContent());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report_signature WHERE report_id=?", Integer.class, reportId)).isEqualTo(1);
        mvc.perform(delete("/api/reports/tbm/{id}/signatures/{s}", reportId, sig).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isNotFound()).andExpect(jsonPath("$.code").value("SIGNATURE_NOT_FOUND"));

        jdbc.update("UPDATE work_unit SET status='IN_PROGRESS', actual_started_at=now() WHERE work_id=?", workId);
        mvc.perform(delete("/api/reports/tbm/{id}/signatures/{s}", reportId, other).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
    }

    @Test
    void 제출은_작업_종료_뒤에만_되고_양식과_서명_목록으로_잠근다() throws Exception {
        signature(reportId, 1);
        submit(admin).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
        submit(viewer).andExpect(status().isForbidden());

        endWork();
        JsonNode submitted = body(submit(admin).andExpect(status().isOk()));
        assertThat(submitted.path("submitted_at").isNumber()).isTrue();
        assertThat(submitted.path("submitted_by_name").asString()).isEqualTo("제출 계정");
        assertThat(submitted.path("locked_at").isNumber()).isTrue();
        assertThat(submitted.path("content_sha256").asString()).startsWith("sha256:");

        // 달력 진행 칩이 완료가 된다
        mvc.perform(get("/api/work-units/{id}", workId).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(jsonPath("$.progress").value("SUBMITTED"))
                .andExpect(jsonPath("$.tbm_report.submitted_at").isNumber());

        // 제출 뒤에는 양식·서명·지우기·다시 제출 모두 잠김
        ObjectNode b = mapper.createObjectNode();
        b.set("form", submitted.path("form"));
        b.put("form_revision", submitted.path("form_revision").asInt());
        mvc.perform(put("/api/reports/tbm/{id}/form", reportId).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(b)))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REPORT_LOCKED"));
        mvc.perform(post("/api/reports/tbm/{id}/signatures", reportId).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(Map.of("report_version", 1,
                                "signer_name", "늦은 서명", "image_key", "signatures/x.png", "image_sha256", "a".repeat(64)))))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REPORT_LOCKED"));
        long sig = jdbc.queryForObject("SELECT signature_id FROM report_signature WHERE report_id=? LIMIT 1", Long.class, reportId);
        mvc.perform(delete("/api/reports/tbm/{id}/signatures/{s}", reportId, sig).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REPORT_LOCKED"));
        submit(admin).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REPORT_LOCKED"));
    }

    @Test
    void 서명_목록이_다르면_잠금_해시도_다르다() throws Exception {
        signature(reportId, 1);
        endWork();
        String withOne = body(submit(admin).andExpect(status().isOk())).path("content_sha256").asString();

        jdbc.update("UPDATE report SET submitted_at=NULL, submitted_by=NULL, locked_at=NULL, content_sha256=NULL WHERE report_id=?", reportId);
        signature(reportId, 1);
        String withTwo = body(submit(admin).andExpect(status().isOk())).path("content_sha256").asString();
        assertThat(withTwo).isNotEqualTo(withOne);
    }

    @Test
    void 결재_서명과_예전_버전_서명은_지우지_않는다() throws Exception {
        long approver = jdbc.queryForObject("""
                INSERT INTO report_signature(report_id, report_version, signer_name, signature_image_key, image_sha256,
                    report_content_sha256, legal_notice_ack, recorded_by, signed_at, signer_role)
                VALUES (?, 1, '결재', ?, ?, 'sha256:x', false, ?, now(), 'APPROVER') RETURNING signature_id
                """, Long.class, reportId, "sig-" + UUID.randomUUID(), "a".repeat(64), adminId);
        mvc.perform(delete("/api/reports/tbm/{id}/signatures/{s}", reportId, approver).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isNotFound());

        long old = signature(reportId, 1);
        jdbc.update("INSERT INTO report(report_kind, work_id, report_version, status) VALUES ('TBM', ?, 2, 'SUCCEEDED')", workId);
        mvc.perform(delete("/api/reports/tbm/{id}/signatures/{s}", reportId, old).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REPORT_VERSION_STALE"));
    }

    @Test
    void 생성_중이면_제출하지_않고_제출_뒤에는_새_버전을_만들지_않는다() throws Exception {
        endWork();
        jdbc.update("UPDATE report SET wrap_up_status='GENERATING' WHERE report_id=?", reportId);
        submit(admin).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REPORT_NOT_READY"));
        jdbc.update("UPDATE report SET wrap_up_status='SUCCEEDED' WHERE report_id=?", reportId);
        submit(admin).andExpect(status().isOk());
        mvc.perform(post("/api/reports/tbm").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"work_id\":" + workId + "}"))
                .andExpect(status().isConflict());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report WHERE work_id=?", Integer.class, workId)).isEqualTo(1);
    }

    @Test
    void 첫_서명으로_잠긴_예전_보고서는_잠금_기록을_덮어쓰지_않는다() throws Exception {
        jdbc.update("UPDATE report SET locked_at = now() - interval '1 day', content_sha256 = 'sha256:old' WHERE report_id=?", reportId);
        endWork();
        JsonNode submitted = body(submit(admin).andExpect(status().isOk()));
        assertThat(submitted.path("content_sha256").asString()).isEqualTo("sha256:old");
        assertThat(submitted.path("submitted_at").asLong()).isGreaterThan(submitted.path("locked_at").asLong());
    }

    private void endWork() {
        jdbc.update("UPDATE work_unit SET status='DONE', actual_started_at=now() - interval '1 hour', actual_ended_at=now() WHERE work_id=?", workId);
    }

    private ResultActions submit(String token) throws Exception {
        return mvc.perform(post("/api/reports/tbm/{id}/submit", reportId).header(HttpHeaders.AUTHORIZATION, token));
    }

    private long signature(long report, int version) {
        return jdbc.queryForObject("""
                INSERT INTO report_signature(report_id, report_version, signer_name, signature_image_key, image_sha256,
                    report_content_sha256, legal_notice_ack, recorded_by, signed_at)
                VALUES (?, ?, '참석자', ?, ?, 'sha256:x', false, ?, now()) RETURNING signature_id
                """, Long.class, report, version, "sig-" + UUID.randomUUID(), "a".repeat(64), adminId);
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
