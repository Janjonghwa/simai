package com.ssafy.simai.domain.rule;

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
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 규칙 버전 조회(GET /api/rules, S15P31A101-264, 명세 7.1·11.4-1·12.6, FR-113). 현재 규칙은 Tier 0 에 보낸 마지막 구성 묶음이 실은
 * 판이다(config_bundle_item). 묶음이 아직 없으면 묶음을 만들 때와 같은 방식으로 운영 프로필의 유효 판을 고른다(ConfigBundleService).
 * R1~R10 은 활성 여부와 함께 모두 싣고(확장 규칙은 비활성), 적용 기간은 versions 에 모든 판의 valid_from·valid_to 로 싣는다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class RuleViews {

    private static final String OPERATION = "OPERATION";
    private static final String COLUMNS = """
            r.rule_id, r.rule_version, r.profile, r.thresholds::text AS thresholds, r.default_severity, r.legal_basis, r.reads,
            r.valid_from, r.valid_to
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    RuleViews(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    ObjectNode rules() {
        List<Map<String, Object>> bundles = jdbc.queryForList(
                "SELECT config_version, rule_profile FROM config_bundle ORDER BY issued_at DESC LIMIT 1", Map.of());
        String configVersion = bundles.isEmpty() ? null : (String) bundles.getFirst().get("config_version");
        // V6 전 묶음은 프로필이 비어 있다. 그때는 운영이었다
        String profile = bundles.isEmpty() || bundles.getFirst().get("rule_profile") == null
                ? OPERATION : (String) bundles.getFirst().get("rule_profile");
        List<ObjectNode> selected = configVersion != null
                ? jdbc.query("SELECT " + COLUMNS + """
                        FROM config_bundle_item i JOIN rule_version r ON r.rule_id = i.item_id AND r.rule_version = i.item_version
                        WHERE i.config_version = :v AND i.item_type = 'RULE'
                        """, new MapSqlParameterSource("v", configVersion), (rs, i) -> version(rs, true))
                // 규칙 ID 마다 하나. ConfigBundleService.rules 와 같은 선택(운영 프로필이면 운영 판)
                : jdbc.query("SELECT DISTINCT ON (r.rule_id) " + COLUMNS + """
                        FROM rule_version r WHERE r.valid_to IS NULL AND r.profile = :profile
                        ORDER BY r.rule_id, r.rule_version DESC
                        """, new MapSqlParameterSource("profile", OPERATION), (rs, i) -> version(rs, true));
        Map<String, ObjectNode> byRule = new LinkedHashMap<>();
        selected.forEach(v -> byRule.put(v.get("rule_id").asString(), v));

        ObjectNode node = mapper.createObjectNode();
        node.put("rule_profile", profile);
        node.put("config_version", configVersion);
        ArrayNode rules = node.putArray("rules");
        for (RuleCatalog rule : RuleCatalog.values()) {
            rules.add(entry(rule.name(), rule.title(), rule.stage(), byRule.remove(rule.name()), selected));
        }
        // 등록부에 7.1 표 밖의 규칙이 생기면 이름 없이 싣는다(숨기지 않는다)
        byRule.forEach((id, v) -> rules.add(entry(id, null, null, v, selected)));
        node.putArray("versions").addAll(jdbc.query("SELECT " + COLUMNS + """
                FROM rule_version r ORDER BY length(r.rule_id), r.rule_id, r.rule_version
                """, Map.of(), (rs, i) -> version(rs, false)));
        return node;
    }

    private ObjectNode entry(String ruleId, String title, String stage, ObjectNode current, List<ObjectNode> selected) {
        ObjectNode n = mapper.createObjectNode();
        n.put("rule_id", ruleId);
        n.put("name", title);
        n.put("stage", stage);
        if (RuleCatalog.WRAPPER.equals(ruleId)) {
            // R6 은 감싼 규칙들의 thresholds.r6 로 적용된다
            List<String> wrapped = new ArrayList<>();
            selected.forEach(v -> {
                if (v.get("thresholds").has("r6")) {
                    wrapped.add(v.get("rule_id").asString());
                }
            });
            n.put("active", !wrapped.isEmpty());
            n.putNull("current");
            ArrayNode with = n.putArray("applied_with");
            wrapped.forEach(with::add);
            return n;
        }
        n.put("active", current != null);
        n.set("current", current == null ? mapper.nullNode() : current);
        return n;
    }

    /** withValues 가 false 면 적용 기간만(versions 목록) */
    private ObjectNode version(ResultSet rs, boolean withValues) throws SQLException {
        ObjectNode v = mapper.createObjectNode();
        v.put("rule_id", rs.getString("rule_id"));
        v.put("rule_version", rs.getInt("rule_version"));
        v.put("profile", rs.getString("profile"));
        if (withValues) {
            JsonNode thresholds = mapper.readTree(rs.getString("thresholds"));
            v.set("thresholds", thresholds);
            v.put("default_severity", rs.getString("default_severity"));
            v.put("legal_basis", rs.getString("legal_basis"));
            ArrayNode reads = v.putArray("reads");
            for (String read : (String[]) rs.getArray("reads").getArray()) {
                reads.add(read);
            }
        }
        v.put("valid_from_ms", millis(rs, "valid_from"));
        v.put("valid_to_ms", millis(rs, "valid_to"));
        return v;
    }

    private static Long millis(ResultSet rs, String column) throws SQLException {
        OffsetDateTime t = rs.getObject(column, OffsetDateTime.class);
        return t == null ? null : t.toInstant().toEpochMilli();
    }
}
