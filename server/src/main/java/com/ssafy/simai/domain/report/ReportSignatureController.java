package com.ssafy.simai.domain.report;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.sql.Timestamp;
import java.time.Clock;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

@RestController
@RequestMapping("/api/reports/tbm/{reportId}/signatures")
@PreAuthorize("hasRole('ADMIN')")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class ReportSignatureController {
    /** 보고서 버전당 서명 인원 상한(설계 문서 work-session-flow.md) */
    private static final int MAX_SIGNERS = 50;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectStorage storage;
    private final ObjectMapper mapper;
    private final SiteLookup sites;
    private final Clock clock;
    ReportSignatureController(NamedParameterJdbcTemplate jdbc, ObjectStorage storage, ObjectMapper mapper, SiteLookup sites, Clock clock) {
        this.jdbc = jdbc; this.storage = storage; this.mapper = mapper; this.sites = sites; this.clock = clock;
    }
    record Request(@JsonProperty("report_version") Integer version, @JsonProperty("signer_name") String name,
            @JsonProperty("image_key") String key, @JsonProperty("image_sha256") String sha,
            @JsonProperty("notice_acknowledged") Boolean notice, @JsonProperty("signer_role") String role) {
        @Override public String toString() { return "SignatureRequest[redacted]"; }
    }
    record Version(int number, String status, Timestamp locked, Timestamp submitted, String sections, String citations,
            String wrap, String form, int latest, Timestamp approved) {}

    /**
     * 참석자 서명(작업 전 확인)·결재. 보고서가 있고 제출 전이면 받는다(S15P31A101-611: 초안 상태와 무관, 첫 서명에 잠그지 않음).
     * 서명마다 그때의 내용 해시(양식·섹션·인용·마무리)를 남긴다. 잠금(locked_at·content_sha256)은 제출 때 한다.
     */
    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @Transactional
    ObjectNode sign(@PathVariable long reportId, @RequestBody Request request, Authentication auth) {
        String role = request.role() == null ? "PARTICIPANT" : request.role();
        if (request.version() == null || request.version() < 1 || request.name() == null || request.name().isBlank()
                || !(role.equals("PARTICIPANT") || role.equals("APPROVER"))
                || request.name().codePointCount(0, request.name().length()) > 40
                || request.key() == null || request.sha() == null || !request.sha().matches("(?:sha256:)?[0-9a-f]{64}")) {
            throw new BusinessException(ErrorCode.Report.INVALID_SIGNATURE);
        }
        long user = userId(auth);
        MapSqlParameterSource p = lockReport(reportId);
        List<Version> rows = jdbc.query("""
                SELECT r.report_version, r.status, r.locked_at, r.submitted_at, r.sections::text, r.citations::text,
                    to_jsonb(r)->'wrap_up_summary', r.form::text,
                    (SELECT max(report_version) FROM report WHERE work_id=r.work_id), r.approved_at
                FROM report r WHERE r.report_id=:id FOR UPDATE
                """, p, (rs, i) -> new Version(rs.getInt(1), rs.getString(2), rs.getTimestamp(3), rs.getTimestamp(4),
                        rs.getString(5), rs.getString(6), rs.getString(7), rs.getString(8), rs.getInt(9), rs.getTimestamp(10)));
        Version v = rows.getFirst();
        if (v.submitted != null) throw new BusinessException(ErrorCode.Report.REPORT_LOCKED);
        if (v.approved != null) throw new BusinessException(ErrorCode.Report.REPORT_APPROVED);
        if (v.number != request.version() || v.number != v.latest) throw new BusinessException(ErrorCode.Report.REPORT_VERSION_STALE);
        String sha = request.sha().replaceFirst("^sha256:", "");
        p.addValue("key", request.key()).addValue("user", user).addValue("sha", sha);
        List<Long> grants = jdbc.queryForList("""
                SELECT grant_id FROM upload_grant WHERE object_key=:key AND purpose='REPORT_SIGNATURE'
                AND requester='USER' AND requested_by_user=:user AND sha256=:sha AND used_at IS NULL FOR UPDATE
                """, p, Long.class);
        if (grants.isEmpty()) throw new BusinessException(ErrorCode.Report.INVALID_SIGNATURE);
        // 보고서 버전당 서명 인원 상한(docs/design/work-session-flow.md '전자서명 이미지 규격', 50명)
        Integer signed = jdbc.queryForObject("SELECT count(*) FROM report_signature WHERE report_id=:id AND signer_role='PARTICIPANT'", p, Integer.class);
        if (role.equals("PARTICIPANT") && signed != null && signed >= MAX_SIGNERS) throw new BusinessException(ErrorCode.Report.SIGNATURE_LIMIT_REACHED);
        Timestamp now = Timestamp.from(clock.instant()); p.addValue("now", now);
        ObjectNode content = mapper.createObjectNode();
        content.set("sections", mapper.readTree(v.sections)); content.set("citations", mapper.readTree(v.citations));
        if (v.wrap == null) content.putNull("wrap_up_summary"); else content.set("wrap_up_summary", mapper.readTree(v.wrap));
        if (v.form == null) content.putNull("form"); else content.set("form", mapper.readTree(v.form));
        String hash = ReportHash.of(mapper, content);
        p.addValue("hash", hash).addValue("version", v.number).addValue("name", request.name().strip()).addValue("role", role)
                .addValue("notice", Boolean.TRUE.equals(request.notice()));
        Long id = jdbc.queryForObject("""
                INSERT INTO report_signature(report_id, report_version, signer_name, signature_image_key, image_sha256,
                    report_content_sha256, legal_notice_ack, recorded_by, signed_at, signer_role)
                VALUES (:id, :version, :name, :key, :sha, :hash, :notice, :user, :now, :role) RETURNING signature_id
                """, p, Long.class);
        if (role.equals("APPROVER")) {
            // 결재자는 요청 인증 계정으로만 기록한다. 실명 입력은 계정 표시명과 별도다.
            jdbc.update("UPDATE report SET approved_at=:now, approved_by=:user WHERE report_id=:id", p);
        }
        jdbc.update("UPDATE upload_grant SET used_at=:now WHERE grant_id=:grant", p.addValue("grant", grants.getFirst()));
        ObjectNode out = mapper.createObjectNode(); out.put("signature_id", id); out.put("report_id", reportId);
        out.put("report_version", v.number); out.put("signer_name", request.name().strip()); out.put("signer_role", role); out.put("signed_at", now.getTime());
        if (v.locked == null) out.putNull("locked_at"); else out.put("locked_at", v.locked.getTime());
        out.put("content_sha256", hash); return out;
    }

    /**
     * 잘못 받은 서명을 지운다(S15P31A101-611). 작업 시작 전(PLANNED)이고 제출 전일 때만. 서명 이미지 객체는 남긴다(보관 정책은 저장소 쪽).
     */
    @DeleteMapping("/{signatureId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    @Transactional
    void delete(@PathVariable long reportId, @PathVariable long signatureId) {
        MapSqlParameterSource p = new MapSqlParameterSource("id", reportId).addValue("site", sites.siteId()).addValue("sig", signatureId);
        record State(String workStatus, Timestamp submitted, boolean latest) {}
        List<State> rows = jdbc.query("""
                SELECT w.status, r.submitted_at,
                       r.report_version = (SELECT max(x.report_version) FROM report x WHERE x.work_id = r.work_id)
                FROM work_unit w JOIN report r ON r.work_id=w.work_id
                WHERE r.report_id=:id AND r.report_kind='TBM' AND w.site_id=:site FOR UPDATE OF w
                """, p, (rs, i) -> new State(rs.getString(1), rs.getTimestamp(2), rs.getBoolean(3)));
        if (rows.isEmpty()) throw new BusinessException(ErrorCode.Report.REPORT_NOT_FOUND);
        State st = rows.getFirst();
        if (st.submitted() != null) throw new BusinessException(ErrorCode.Report.REPORT_LOCKED);
        if (!st.latest()) throw new BusinessException(ErrorCode.Report.REPORT_VERSION_STALE);
        if (!"PLANNED".equals(st.workStatus())) throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
        // 참석자 서명만 지운다. 결재 서명은 report.approved_at 과 짝이라 지우면 결재 표시만 남는다
        int deleted = jdbc.update("DELETE FROM report_signature WHERE signature_id=:sig AND report_id=:id AND signer_role='PARTICIPANT'", p);
        if (deleted == 0) throw new BusinessException(ErrorCode.Report.SIGNATURE_NOT_FOUND);
    }

    /** 이 현장의 TBM 보고서인지 보고 작업 행을 잠근다(보고서 생성·작업 시작과 같은 잠금 순서). */
    private MapSqlParameterSource lockReport(long reportId) {
        MapSqlParameterSource p = new MapSqlParameterSource("id", reportId).addValue("site", sites.siteId());
        List<Long> works = jdbc.queryForList("""
                SELECT w.work_id FROM work_unit w JOIN report r ON r.work_id=w.work_id
                WHERE r.report_id=:id AND r.report_kind='TBM' AND w.site_id=:site FOR UPDATE OF w
                """, p, Long.class);
        if (works.isEmpty()) throw new BusinessException(ErrorCode.Report.REPORT_NOT_FOUND);
        return p;
    }

    private static long userId(Authentication auth) {
        try { return Long.parseLong(auth.getName()); } catch (NumberFormatException e) { throw new BusinessException(ErrorCode.Auth.FORBIDDEN); }
    }

    @GetMapping
    @Transactional(readOnly = true)
    ObjectNode list(@PathVariable long reportId, @RequestParam(name="version", required=false) Integer version) {
        if (version != null && version < 1) throw new BusinessException(ErrorCode.Report.INVALID_SIGNATURE);
        MapSqlParameterSource p = new MapSqlParameterSource("id", reportId).addValue("site", sites.siteId());
        List<Long> works = jdbc.queryForList("""
                SELECT r.work_id FROM report r JOIN work_unit w ON w.work_id=r.work_id
                WHERE r.report_id=:id AND r.report_kind='TBM' AND w.site_id=:site
                """, p, Long.class);
        if (works.isEmpty()) throw new BusinessException(ErrorCode.Report.REPORT_NOT_FOUND);
        p.addValue("work", works.getFirst()).addValue("version", version);
        ObjectNode out = mapper.createObjectNode(); ArrayNode items = out.putArray("items");
        jdbc.query("""
                SELECT s.signature_id, s.signer_name, s.signed_at, s.report_version, s.signature_image_key, s.signer_role
                FROM report_signature s JOIN report r ON r.report_id=s.report_id
                WHERE r.work_id=:work AND ((CAST(:version AS integer) IS NULL AND r.report_id=:id) OR r.report_version=:version)
                ORDER BY s.signature_id
                """, p, rs -> {
            ObjectNode item = items.addObject(); item.put("signature_id", rs.getLong(1)); item.put("signer_name", rs.getString(2));
            item.put("signed_at", rs.getTimestamp(3).getTime()); item.put("report_version", rs.getInt(4));
            item.put("image_url", storage.signGet(rs.getString(5)).url());
            item.put("signer_role", rs.getString(6));
        }); return out;
    }
}
