package com.ssafy.simai.domain.rule;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.config.Versioned;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 규칙 버전 조회(GET /api/rules, S15P31A101-264, 명세 7.1·11.4-1·12.6, FR-113). 현재 규칙은 Tier 0 가 적용했다고 알린(config_ack APPLIED,
 * config_bundle.applied_at) 이 현장의 마지막 구성 묶음이 실은 판이다(config_bundle_item). 발행했지만 아직 적용 전이거나 거부된 묶음은
 * 현재가 아니고 pending_config_version 으로만 알린다. 적용된 묶음이 없으면 묶음을 만들 때와 같은 선택(RuleSelection)으로 운영 판을 준다.
 * R1~R11 은 활성 여부와 함께 모두 싣고(확장 규칙은 비활성), 적용 기간은 versions 에 모든 판의 valid_from·valid_to 로 싣는다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class RuleViews {

    private static final String OPERATION = "OPERATION";
    private static final String COLUMNS = """
            r.rule_id, r.rule_version, r.profile, r.thresholds::text AS thresholds, r.default_severity, r.legal_basis, r.reads,
            r.valid_from, r.valid_to
            """;
    /** 규칙 ID 를 R1, R2, …, R11 순서로(문자열 순이면 R10 이 R2 앞에 온다) */
    private static final String RULE_ORDER = "length(r.rule_id), r.rule_id";

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final SiteLookup sites;

    RuleViews(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, SiteLookup sites) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.sites = sites;
    }

    ObjectNode rules() {
        MapSqlParameterSource site = new MapSqlParameterSource("site", sites.siteId());
        List<Map<String, Object>> applied = jdbc.queryForList("""
                SELECT config_version, rule_profile FROM config_bundle
                WHERE site_id = :site AND applied_at IS NOT NULL ORDER BY applied_at DESC, issued_at DESC LIMIT 1
                """, site);
        String configVersion = applied.isEmpty() ? null : (String) applied.getFirst().get("config_version");
        // V6 전 묶음은 프로필이 비어 있다. 그때는 운영이었다
        String profile = applied.isEmpty() || applied.getFirst().get("rule_profile") == null
                ? OPERATION : (String) applied.getFirst().get("rule_profile");
        List<ObjectNode> selected = configVersion != null
                ? jdbc.query("SELECT " + COLUMNS + """
                        FROM config_bundle_item i JOIN rule_version r ON r.rule_id = i.item_id AND r.rule_version = i.item_version
                        WHERE i.config_version = :v AND i.item_type = 'RULE' ORDER BY
                        """ + RULE_ORDER, new MapSqlParameterSource("v", configVersion), (rs, i) -> current(rs))
                : jdbc.query("SELECT " + COLUMNS + " FROM (" + RuleSelection.ACTIVE_FOR_PROFILE + ") r ORDER BY " + RULE_ORDER,
                        new MapSqlParameterSource("profile", OPERATION), (rs, i) -> current(rs));
        Map<String, ObjectNode> byRule = new LinkedHashMap<>();
        selected.forEach(v -> byRule.put(v.get("rule_id").asString(), v));

        ObjectNode node = mapper.createObjectNode();
        node.put("rule_profile", profile);
        node.put("config_version", configVersion);
        // 발행했지만 Tier 0 가 아직 적용했다고 알리지 않은(또는 거부한) 더 새 묶음. 현재 규칙이 아니다
        node.put("pending_config_version", jdbc.queryForList("""
                SELECT config_version FROM config_bundle WHERE site_id = :site AND applied_at IS NULL
                    AND issued_at > COALESCE((SELECT max(issued_at) FROM config_bundle WHERE site_id = :site AND applied_at IS NOT NULL),
                        '-infinity'::timestamptz)
                ORDER BY issued_at DESC LIMIT 1
                """, site, String.class).stream().findFirst().orElse(null));
        ArrayNode rules = node.putArray("rules");
        for (RuleCatalog rule : RuleCatalog.values()) {
            rules.add(entry(rule.name(), rule.title(), rule.stage(), byRule.remove(rule.name()), selected));
        }
        // 등록부에 7.1 표 밖의 규칙이 생기면 이름 없이 싣는다(숨기지 않는다)
        byRule.forEach((id, v) -> rules.add(entry(id, null, null, v, selected)));
        // 적용 기간만 싣는다(값은 current 에만)
        node.putArray("versions").addAll(jdbc.query("""
                SELECT r.rule_id, r.rule_version, r.profile, r.valid_from, r.valid_to FROM rule_version r ORDER BY
                """ + RULE_ORDER + ", r.rule_version", Map.of(), (rs, i) -> period(rs)));
        return node;
    }

    private ObjectNode entry(String ruleId, String title, String stage, ObjectNode current, List<ObjectNode> selected) {
        ObjectNode n = mapper.createObjectNode();
        n.put("rule_id", ruleId);
        n.put("name", title);
        n.put("stage", stage);
        n.set("current", current == null ? mapper.nullNode() : current);
        if (!RuleCatalog.WRAPPER.equals(ruleId)) {
            n.put("active", current != null);
            return n;
        }
        // R6 은 감싼 규칙들의 thresholds.r6(설정 객체)로 적용된다. 등록부가 R6 자기 판을 두면 그 판도 current 로 싣는다
        List<String> wrapped = new ArrayList<>();
        selected.forEach(v -> {
            if (v.get("thresholds").path("r6").isObject()) {
                wrapped.add(v.get("rule_id").asString());
            }
        });
        n.put("active", current != null || !wrapped.isEmpty());
        ArrayNode with = n.putArray("applied_with");
        wrapped.forEach(with::add);
        return n;
    }

    private ObjectNode current(ResultSet rs) throws SQLException {
        ObjectNode v = period(rs);
        v.set("thresholds", mapper.readTree(rs.getString("thresholds")));
        v.put("default_severity", rs.getString("default_severity"));
        v.put("legal_basis", rs.getString("legal_basis"));
        ArrayNode reads = v.putArray("reads");
        for (String read : (String[]) rs.getArray("reads").getArray()) {
            reads.add(read);
        }
        return v;
    }

    private ObjectNode period(ResultSet rs) throws SQLException {
        ObjectNode v = mapper.createObjectNode();
        v.put("rule_id", rs.getString("rule_id"));
        v.put("rule_version", rs.getInt("rule_version"));
        v.put("profile", rs.getString("profile"));
        v.put("valid_from_ms", Versioned.millis(rs.getObject("valid_from", OffsetDateTime.class)));
        v.put("valid_to_ms", Versioned.millis(rs.getObject("valid_to", OffsetDateTime.class)));
        return v;
    }
}
