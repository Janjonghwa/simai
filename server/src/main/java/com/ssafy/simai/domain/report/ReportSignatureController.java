package com.ssafy.simai.domain.report;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Timestamp;
import java.time.Clock;
import java.util.HexFormat;
import java.util.List;
import java.util.TreeMap;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.JsonNode;
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
    record Version(int number, String status, Timestamp locked, String hash, String sections, String citations, String wrap, int latest, Timestamp approved) {}

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @Transactional
    ObjectNode sign(@PathVariable long reportId, @RequestBody Request request, Authentication auth) {
        String role = request.role() == null ? "PARTICIPANT" : request.role();
        if (request.version() == null || request.version() < 1 || request.name() == null || request.name().isBlank()
                || !(role.equals("PARTICIPANT") || role.equals("APPROVER"))
                || request.name().codePointCount(0, request.name().length()) > 40 || !Boolean.TRUE.equals(request.notice())
                || request.key() == null || request.sha() == null || !request.sha().matches("(?:sha256:)?[0-9a-f]{64}")) {
            throw new BusinessException(ErrorCode.Report.INVALID_SIGNATURE);
        }
        long user;
        try { user = Long.parseLong(auth.getName()); } catch (NumberFormatException e) { throw new BusinessException(ErrorCode.Auth.FORBIDDEN); }
        MapSqlParameterSource p = new MapSqlParameterSource("id", reportId).addValue("site", sites.siteId());
        List<Long> works = jdbc.queryForList("""
                SELECT w.work_id FROM work_unit w JOIN report r ON r.work_id=w.work_id
                WHERE r.report_id=:id AND r.report_kind='TBM' AND w.site_id=:site FOR UPDATE OF w
                """, p, Long.class);
        if (works.isEmpty()) throw new BusinessException(ErrorCode.Report.REPORT_NOT_FOUND);
        List<Version> rows = jdbc.query("""
                SELECT r.report_version, r.status, r.locked_at, r.content_sha256, r.sections::text, r.citations::text,
                    to_jsonb(r)->'wrap_up_summary', (SELECT max(report_version) FROM report WHERE work_id=r.work_id), r.approved_at
                FROM report r WHERE r.report_id=:id FOR UPDATE
                """, p, (rs, i) -> new Version(rs.getInt(1), rs.getString(2), rs.getTimestamp(3), rs.getString(4),
                        rs.getString(5), rs.getString(6), rs.getString(7), rs.getInt(8), rs.getTimestamp(9)));
        Version v = rows.getFirst();
        if (v.approved != null) throw new BusinessException(ErrorCode.Report.REPORT_APPROVED);
        if (v.number != request.version() || v.number != v.latest) throw new BusinessException(ErrorCode.Report.REPORT_VERSION_STALE);
        if (!v.status.equals("SUCCEEDED")) throw new BusinessException(ErrorCode.Report.REPORT_NOT_READY);
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
        String hash = v.hash; Timestamp locked = v.locked;
        if (locked == null) {
            ObjectNode content = mapper.createObjectNode();
            content.set("sections", mapper.readTree(v.sections)); content.set("citations", mapper.readTree(v.citations));
            if (v.wrap == null) content.putNull("wrap_up_summary"); else content.set("wrap_up_summary", mapper.readTree(v.wrap));
            hash = digest(mapper.writeValueAsString(sorted(content))); locked = now;
            jdbc.update("UPDATE report SET locked_at=:now, content_sha256=:hash WHERE report_id=:id", p.addValue("hash", hash));
        }
        p.addValue("hash", hash).addValue("version", v.number).addValue("name", request.name().strip()).addValue("role", role);
        Long id = jdbc.queryForObject("""
                INSERT INTO report_signature(report_id, report_version, signer_name, signature_image_key, image_sha256,
                    report_content_sha256, legal_notice_ack, recorded_by, signed_at, signer_role)
                VALUES (:id, :version, :name, :key, :sha, :hash, true, :user, :now, :role) RETURNING signature_id
                """, p, Long.class);
        if (role.equals("APPROVER")) {
            // 결재자는 요청 인증 계정으로만 기록한다. 실명 입력은 계정 표시명과 별도다.
            jdbc.update("UPDATE report SET approved_at=:now, approved_by=:user WHERE report_id=:id", p);
        }
        jdbc.update("UPDATE upload_grant SET used_at=:now WHERE grant_id=:grant", p.addValue("grant", grants.getFirst()));
        ObjectNode out = mapper.createObjectNode(); out.put("signature_id", id); out.put("report_id", reportId);
        out.put("report_version", v.number); out.put("signer_name", request.name().strip()); out.put("signer_role", role); out.put("signed_at", now.getTime());
        out.put("locked_at", locked.getTime()); out.put("content_sha256", hash); return out;
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
    private JsonNode sorted(JsonNode n) {
        if (n.isObject()) {
            TreeMap<String, JsonNode> entries = new TreeMap<>(); n.properties().forEach(e -> entries.put(e.getKey(), e.getValue()));
            ObjectNode result = mapper.createObjectNode(); entries.forEach((k, v) -> result.set(k, sorted(v))); return result;
        }
        if (n.isArray()) { ArrayNode result = mapper.createArrayNode(); n.forEach(v -> result.add(sorted(v))); return result; }
        return n;
    }
    private static String digest(String value) {
        try { return "sha256:" + HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8))); }
        catch (NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }
}
