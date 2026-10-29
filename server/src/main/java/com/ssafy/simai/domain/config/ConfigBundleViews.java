package com.ssafy.simai.domain.config;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 구성 묶음 적용 상태(GET /api/config-bundles). status 는 가장 늦게 받은 config_ack 로 정한다: 없으면 PENDING(적용 대기),
 * APPLIED·REJECTED 는 그 응답대로, APPLIED 인데 해시가 발행한 것과 다르면 MISMATCH 다. 화면에는 내려받기 주소(서명)를 싣지 않는다.
 * items 는 발행 때 묶음에 넣은 구역·규칙·작업면·정적 객체의 ID·버전(config_bundle_item)이다. 화면이 항목마다 어느 묶음에 실렸는지,
 * 그 묶음이 현장에 적용됐는지를 시각 비교 없이 맞춘다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ConfigBundleViews {

    private static final String SELECT = """
            SELECT b.config_version, b.previous_config_version, b.issued_at, b.bundle_hash, b.components::text AS components,
                b.calibration_version, b.rule_profile, b.applied_at,
                a.result, a.bundle_hash AS ack_hash, a.applied_at AS ack_applied_at, a.reject_reason, a.reject_detail,
                a.active_config_version, a.gateway_id, a.received_at
            FROM config_bundle b
            LEFT JOIN LATERAL (SELECT * FROM config_ack c WHERE c.config_version = b.config_version
                ORDER BY c.received_at DESC LIMIT 1) a ON true
            """;

    // 작업면과 장애물을 화면이 가를 수 있게 정적 객체는 그 버전의 kind 를 붙인다
    private static final String ITEMS = """
            SELECT i.config_version, i.item_type, i.item_id, i.item_version, s.kind
            FROM config_bundle_item i
            LEFT JOIN static_object s ON i.item_type = 'STATIC_OBJECT' AND s.object_id = i.item_id AND s.object_version = i.item_version
            WHERE i.config_version IN (:versions)
            ORDER BY i.config_version, i.item_type, i.item_id
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public ConfigBundleViews(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    public ObjectNode find(String configVersion) {
        ObjectNode bundle = jdbc.query(SELECT + " WHERE b.config_version = :v", new MapSqlParameterSource("v", configVersion),
                (rs, i) -> view(rs)).stream().findFirst()
                .orElseThrow(() -> new BusinessException(ErrorCode.Config.CONFIG_BUNDLE_NOT_FOUND));
        attachItems(List.of(bundle));
        return bundle;
    }

    public List<ObjectNode> recent(int limit) {
        List<ObjectNode> bundles = jdbc.query(SELECT + " ORDER BY b.issued_at DESC LIMIT :limit",
                new MapSqlParameterSource("limit", limit), (rs, i) -> view(rs));
        attachItems(bundles);
        return bundles;
    }

    /** 묶음마다 items 를 한 번의 조회로 채운다(묶음 수만큼 따로 묻지 않는다) */
    private void attachItems(List<ObjectNode> bundles) {
        if (bundles.isEmpty()) {
            return;
        }
        Map<String, ArrayNode> byVersion = new HashMap<>();
        for (ObjectNode bundle : bundles) {
            byVersion.put(bundle.get("config_version").asString(), bundle.putArray("items"));
        }
        jdbc.query(ITEMS, new MapSqlParameterSource("versions", byVersion.keySet()), rs -> {
            ObjectNode item = byVersion.get(rs.getString("config_version")).addObject();
            item.put("item_type", rs.getString("item_type"));
            item.put("item_id", rs.getString("item_id"));
            item.put("item_version", rs.getInt("item_version"));
            if ("STATIC_OBJECT".equals(rs.getString("item_type"))) {
                item.put("kind", rs.getString("kind"));
            }
        });
    }

    /** Tier 0 에 보낼 config_bundle payload(계약 그대로). 없으면 empty */
    public Optional<ObjectNode> notice(String configVersion) {
        return jdbc.query("""
                SELECT config_version, previous_config_version, issued_at, bundle_uri, bundle_hash, components::text AS components
                FROM config_bundle WHERE config_version = :v
                """, new MapSqlParameterSource("v", configVersion), (rs, i) -> {
            ObjectNode p = mapper.createObjectNode();
            p.put("config_version", rs.getString("config_version"));
            p.put("previous_config_version", rs.getString("previous_config_version"));
            p.put("issued_at", rs.getObject("issued_at", OffsetDateTime.class).toInstant().toEpochMilli());
            p.put("bundle_uri", rs.getString("bundle_uri"));
            p.put("bundle_hash", rs.getString("bundle_hash"));
            p.set("components", mapper.readTree(rs.getString("components")));
            return p;
        }).stream().findFirst();
    }

    public Optional<String> latestVersion() {
        return jdbc.queryForList("SELECT config_version FROM config_bundle ORDER BY issued_at DESC LIMIT 1", Map.of(), String.class)
                .stream().findFirst();
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        ObjectNode b = mapper.createObjectNode();
        b.put("config_version", rs.getString("config_version"));
        b.put("previous_config_version", rs.getString("previous_config_version"));
        b.put("issued_at", millis(rs, "issued_at"));
        b.put("bundle_hash", rs.getString("bundle_hash"));
        b.put("calibration_version", rs.getString("calibration_version"));
        b.put("rule_profile", rs.getString("rule_profile"));
        ObjectNode components = b.putObject("components");
        JsonNode stored = mapper.readTree(rs.getString("components"));
        for (String name : stored.propertyNames()) {
            ObjectNode c = components.putObject(name);
            c.set("version", stored.get(name).get("version"));
            c.set("sha256", stored.get(name).get("sha256"));
        }
        String result = rs.getString("result");
        // APPLIED 인데 Tier 0 가 계산한 해시가 발행한 것과 다르면 현장 적용으로 보지 않는다(applied_at 도 비어 있다)
        boolean mismatch = "APPLIED".equals(result) && !rs.getString("bundle_hash").equals(rs.getString("ack_hash"));
        b.put("status", result == null ? "PENDING" : mismatch ? "MISMATCH" : result);
        b.put("applied_at", millis(rs, "applied_at"));
        if (result == null) {
            b.putNull("last_ack");
        } else {
            ObjectNode ack = b.putObject("last_ack");
            ack.put("result", result);
            ack.put("gateway_id", rs.getString("gateway_id"));
            ack.put("bundle_hash", rs.getString("ack_hash"));
            ack.put("applied_at", millis(rs, "ack_applied_at"));
            ack.put("reject_reason", rs.getString("reject_reason"));
            ack.put("reject_detail", rs.getString("reject_detail"));
            ack.put("active_config_version", rs.getString("active_config_version"));
            ack.put("received_at", millis(rs, "received_at"));
        }
        return b;
    }

    private static Long millis(ResultSet rs, String column) throws SQLException {
        return Versioned.millis(rs.getObject(column, OffsetDateTime.class));
    }
}
