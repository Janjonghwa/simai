package com.ssafy.simai.domain.report;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HexFormat;
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
import org.springframework.security.oauth2.jwt.*;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class ReportSignatureIntegrationTest {
    private static final String SHA = "a".repeat(64);
    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired JwtEncoder encoder;
    @Autowired ObjectMapper mapper;
    @Autowired WorkWrapUpRepository wrapUps;
    long adminId, otherId, workId, reportId;
    String admin, other, viewer;

    @BeforeEach
    void setup() {
        String tag = UUID.randomUUID().toString();
        adminId = user("admin-" + tag, "ADMIN");
        otherId = user("other-" + tag, "ADMIN");
        admin = token(adminId, "ADMIN");
        other = token(otherId, "ADMIN");
        viewer = token(user("viewer-" + tag, "VIEWER"), "VIEWER");
        workId = jdbc.queryForObject("""
                INSERT INTO work_unit(site_id, work_date, name, starts_at, ends_at, planned_people, created_by, created_at, updated_at)
                VALUES ((SELECT site_id FROM site LIMIT 1), CURRENT_DATE, 'test', now(), now() + interval '1 hour', 0, ?, now(), now())
                RETURNING work_id
                """, Long.class, adminId);
        reportId = report(1, "SUCCEEDED");
        jdbc.update("UPDATE report SET sections=CAST(? AS jsonb), citations=CAST(? AS jsonb) WHERE report_id=?",
                "[{\"z\":1,\"a\":{\"y\":2,\"b\":3}}]", "[{\"d\":4,\"c\":5}]", reportId);
    }

    @Test
    void firstSignatureLocksCanonicalContentAndLaterSignaturesReuseHash() throws Exception {
        String key = upload(admin);
        JsonNode first = body(sign(admin, reportId, 1, key, SHA, true).andExpect(status().isCreated()));
        String canonical = "{\"citations\":[{\"c\":5,\"d\":4}],\"sections\":[{\"a\":{\"b\":3,\"y\":2},\"z\":1}],\"wrap_up_summary\":null}";
        String expected = "sha256:" + HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(canonical.getBytes(StandardCharsets.UTF_8)));
        assertThat(first.path("content_sha256").asString()).isEqualTo(expected);
        assertThat(first.path("signer_role").asString()).isEqualTo("PARTICIPANT");
        assertThat(jdbc.queryForObject("SELECT signer_role FROM report_signature WHERE signature_id=?", String.class,
                first.path("signature_id").asLong())).isEqualTo("PARTICIPANT");
        assertThat(jdbc.queryForObject("SELECT content_sha256 FROM report WHERE report_id=?", String.class, reportId)).isEqualTo(expected);
        assertThat(jdbc.queryForObject("SELECT locked_at FROM report WHERE report_id=?", java.sql.Timestamp.class, reportId)).isNotNull();
        JsonNode second = body(sign(admin, reportId, 1, upload(admin), SHA, true).andExpect(status().isCreated()));
        assertThat(second.path("content_sha256").asString()).isEqualTo(expected);
        assertThat(second.path("locked_at").asLong()).isEqualTo(first.path("locked_at").asLong());
        mvc.perform(get("/api/reports/tbm/{id}/signatures", reportId).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.items.length()").value(2))
                .andExpect(jsonPath("$.items[0].image_url").exists())
                .andExpect(jsonPath("$.items[0].signer_role").value("PARTICIPANT"));
        mvc.perform(get("/api/reports/tbm/{id}/signatures", reportId).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isForbidden());
    }

    @Test
    void invalidNoticeForeignGrantAndHashAreRejected() throws Exception {
        String foreign = upload(other);
        sign(admin, reportId, 1, foreign, SHA, true).andExpect(status().isBadRequest());
        String own = upload(admin);
        sign(admin, reportId, 1, own, "b".repeat(64), true).andExpect(status().isBadRequest());
        sign(admin, reportId, 1, own, SHA, false).andExpect(status().isBadRequest());
        assertThat(jdbc.queryForObject("SELECT locked_at FROM report WHERE report_id=?", java.sql.Timestamp.class, reportId)).isNull();
    }

    @Test
    void staleAndGeneratingVersionsAreRejected() throws Exception {
        String key = upload(admin);
        long pending = report(2, "GENERATING");
        sign(admin, reportId, 1, key, SHA, true).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REPORT_VERSION_STALE"));
        sign(admin, pending, 2, key, SHA, true).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REPORT_NOT_READY"));
    }

    @Test
    void uploadRequiresAdminPngAndSizeLimit() throws Exception {
        upload(admin);
        uploadRequest(viewer, "image/png", 100).andExpect(status().isForbidden());
        uploadRequest(admin, "image/jpeg", 100).andExpect(status().isBadRequest());
        uploadRequest(admin, "image/png", (256L << 10) + 1).andExpect(status().isBadRequest());
    }

    @Test
    void approvalReusesHashAndRejectsEveryFurtherSignature() throws Exception {
        JsonNode participant = body(sign(admin, reportId, 1, upload(admin), SHA, true).andExpect(status().isCreated()));
        jdbc.update("UPDATE app_user SET display_label='결재 계정 표시명' WHERE user_id=?", otherId);
        JsonNode approval = body(approve(other, reportId, 1, upload(other)).andExpect(status().isCreated())
                .andExpect(jsonPath("$.signer_role").value("APPROVER")));
        assertThat(approval.path("content_sha256").asString()).isEqualTo(participant.path("content_sha256").asString());
        assertThat(approval.path("locked_at").asLong()).isEqualTo(participant.path("locked_at").asLong());
        assertThat(jdbc.queryForObject("SELECT approved_by FROM report WHERE report_id=?", Long.class, reportId)).isEqualTo(otherId);
        assertThat(jdbc.queryForObject("SELECT recorded_by FROM report_signature WHERE signature_id=?", Long.class,
                approval.path("signature_id").asLong())).isEqualTo(otherId);
        mvc.perform(get("/api/reports/tbm/{id}", reportId).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.approved_at").isNumber())
                .andExpect(jsonPath("$.approved_by_name").value("결재 계정 표시명"));
        mvc.perform(get("/api/reports/tbm/{id}/signatures", reportId).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.items[1].signer_role").value("APPROVER"));
        approve(admin, reportId, 1, upload(admin)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REPORT_APPROVED"));
        sign(admin, reportId, 1, upload(admin), SHA, true).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REPORT_APPROVED"));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report_signature WHERE report_id=? AND signer_role='APPROVER'",
                Integer.class, reportId)).isEqualTo(1);
    }

    @Test
    void approvalWithoutParticipantsLocksContentAndNewVersionRequiresNewApproval() throws Exception {
        JsonNode first = body(approve(admin, reportId, 1, upload(admin)).andExpect(status().isCreated()));
        assertThat(first.path("content_sha256").asString()).startsWith("sha256:");
        assertThat(first.path("locked_at").asLong()).isPositive();
        Instant endedAt = Instant.now();
        var work = mapper.createObjectNode();
        work.put("actual_started_at", endedAt.minusSeconds(60).toEpochMilli());
        work.put("actual_ended_at", endedAt.toEpochMilli());
        work.putNull("zone_id");
        long next = wrapUps.prepare(workId, work, endedAt).path("report_id").asLong();
        wrapUps.complete(next, "작업 마무리", true, null, endedAt);
        mvc.perform(get("/api/reports/tbm/{id}", next).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.approved_at").isEmpty())
                .andExpect(jsonPath("$.approved_by_name").isEmpty()).andExpect(jsonPath("$.locked_at").isEmpty());
        JsonNode second = body(approve(other, next, 2, upload(other)).andExpect(status().isCreated()));
        assertThat(second.path("content_sha256").asString()).isNotEqualTo(first.path("content_sha256").asString());
        assertThat(jdbc.queryForObject("SELECT approved_by FROM report WHERE report_id=?", Long.class, reportId)).isEqualTo(adminId);
        assertThat(jdbc.queryForObject("SELECT approved_by FROM report WHERE report_id=?", Long.class, next)).isEqualTo(otherId);
        approve(admin, reportId, 1, upload(admin)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REPORT_APPROVED"));
    }

    @Test
    void invalidRoleAndForeignApprovalGrantAreRejected() throws Exception {
        approve(admin, reportId, 1, upload(other)).andExpect(status().isBadRequest());
        mvc.perform(post("/api/reports/tbm/{id}/signatures", reportId).header(HttpHeaders.AUTHORIZATION, admin)
                .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(Map.of("report_version", 1,
                        "signer_name", "결재 실명", "signer_role", "OWNER", "image_key", upload(admin),
                        "image_sha256", SHA, "notice_acknowledged", true))))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_SIGNATURE"));
        assertThat(jdbc.queryForObject("SELECT approved_at FROM report WHERE report_id=?", java.sql.Timestamp.class, reportId)).isNull();
    }

    @Test
    void participantDefaultAndLimitStillAllowFinalApproval() throws Exception {
        JsonNode first = body(sign(admin, reportId, 1, upload(admin), SHA, true).andExpect(status().isCreated()));
        jdbc.update("""
                INSERT INTO report_signature(report_id, report_version, signer_name, signature_image_key,
                    image_sha256, report_content_sha256, legal_notice_ack, recorded_by, signed_at)
                SELECT ?, 1, '참석자', 'fixture-' || ? || '-' || n, ?, ?, true, ?, now()
                FROM generate_series(1, 49) n
                """, reportId, reportId, SHA, first.path("content_sha256").asString(), adminId);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report_signature WHERE report_id=? AND signer_role='PARTICIPANT'",
                Integer.class, reportId)).isEqualTo(50);
        sign(admin, reportId, 1, upload(admin), SHA, true).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SIGNATURE_LIMIT_REACHED"));
        approve(admin, reportId, 1, upload(admin)).andExpect(status().isCreated());
    }

    private ResultActions approve(String token, long id, int version, String key) throws Exception {
        return mvc.perform(post("/api/reports/tbm/{id}/signatures", id).header(HttpHeaders.AUTHORIZATION, token)
                .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(Map.of("report_version", version,
                        "signer_name", "결재 실명", "signer_role", "APPROVER", "image_key", key,
                        "image_sha256", SHA, "notice_acknowledged", true))));
    }

    private long user(String login, String role) {
        return jdbc.queryForObject("INSERT INTO app_user(login_id,password_hash,role,created_at) VALUES (?, 'x', ?, now()) RETURNING user_id", Long.class, login, role);
    }
    private long report(int version, String status) {
        return jdbc.queryForObject("INSERT INTO report(report_kind,work_id,report_version,status) VALUES ('TBM',?,?,?) RETURNING report_id", Long.class, workId, version, status);
    }
    private String token(long id, String role) {
        Instant now = Instant.now();
        var claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(id)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return "Bearer " + encoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }
    private ResultActions uploadRequest(String token, String type, long size) throws Exception {
        return mvc.perform(post("/api/uploads/sign").header(HttpHeaders.AUTHORIZATION, token).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(Map.of("purpose", "REPORT_SIGNATURE", "content_type", type, "size_bytes", size, "sha256", SHA))));
    }
    private String upload(String token) throws Exception {
        String key = body(uploadRequest(token, "image/png", 100).andExpect(status().isOk())).path("object_key").asString();
        assertThat(key).matches("signatures/[0-9a-f-]{36}\\.png");
        return key;
    }
    private ResultActions sign(String token, long id, int version, String key, String sha, boolean notice) throws Exception {
        return mvc.perform(post("/api/reports/tbm/{id}/signatures", id).header(HttpHeaders.AUTHORIZATION, token)
                .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(Map.of("report_version", version,
                        "signer_name", "시험 서명자", "image_key", key, "image_sha256", sha, "notice_acknowledged", notice))));
    }
    private JsonNode body(ResultActions result) throws Exception {
        return mapper.readTree(result.andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8));
    }
}
