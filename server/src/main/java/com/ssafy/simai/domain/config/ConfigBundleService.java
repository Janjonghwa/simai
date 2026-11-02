package com.ssafy.simai.domain.config;

import com.ssafy.simai.domain.calibration.CalibrationViews;
import com.ssafy.simai.domain.rule.RuleSelection;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.function.Supplier;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 구성 묶음 발행(명세 3.2 구성 묶음, 11.1, 계약 config_bundle). 발행 때 지금 유효한 구역·규칙·보정·작업면을 묶어 새 config_version 을
 * 만든다(덮어쓰지 않는다). 묶음 파일(매니페스트와 구성 요소 4개)의 바이트·해시를 저장하고, 커밋 뒤 연결된 Tier 0 에 알린다.
 * Tier 0 는 파일을 서명 주소로 내려받아 해시·형식·참조를 검사하고 원자적으로 전환한 뒤 config_ack 로 답한다. 그 전까지는 '적용 대기'다.
 *
 * <p>구성 요소 내용 형식은 계약 범위 밖이라 Tier 0 의 임시 형식(ai/src/simai_ai/config/bundle.py 표)을 따른다.
 * <ul>
 *   <li>zones: {"version", "zones": [Zone.from_json 형식]}. 지금 유효한 버전만 싣는다(valid_from 은 ms, valid_to 는 없음)
 *   <li>rules: {"version", "rule_versions": [...]}. 규칙 ID 마다 하나: 운영(OPERATION) 또는 교실 시험(CLASSROOM_TEST, 그 규칙에
 *       교실 시험 버전이 없으면 운영 버전)
 *   <li>calibration: 가장 최근에 올린 보정 문서 원문
 *   <li>operational_geometry: {"version", "work_surfaces": [...], "static_objects": [...]}. 작업면은 OBB 바닥 투영과 상판 높이
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ConfigBundleService {

    public static final String OPERATION = "OPERATION";
    public static final String CLASSROOM_TEST = "CLASSROOM_TEST";
    static final Set<String> RULE_PROFILES = Set.of(OPERATION, CLASSROOM_TEST);
    private static final String VERSION_PREFIX = "cfg-";

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;
    private final BundleUrlSigner signer;
    private final SiteLookup sites;
    private final ConfigBundleSender sender;
    private final ConfigBundleViews views;
    private final CalibrationViews calibrations;
    private final Clock clock;

    public ConfigBundleService(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, TransactionTemplate transaction,
            BundleUrlSigner signer, SiteLookup sites, ConfigBundleSender sender, ConfigBundleViews views, CalibrationViews calibrations,
            Clock clock) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.transaction = transaction;
        this.signer = signer;
        this.sites = sites;
        this.sender = sender;
        this.views = views;
        this.calibrations = calibrations;
        this.clock = clock;
    }

    /** @param ruleProfile 없으면 이전 묶음의 프로필, 그것도 없으면 OPERATION */
    public ObjectNode publish(String ruleProfile, long userId) {
        if (ruleProfile != null && !RULE_PROFILES.contains(ruleProfile)) {
            throw new BusinessException(ErrorCode.Config.INVALID_RULE_PROFILE);
        }
        if (!signer.configured()) {
            throw new BusinessException(ErrorCode.Config.PUBLIC_URL_NOT_SET);
        }
        String siteId = sites.siteId();
        String configVersion = transaction.execute(status -> build(siteId, ruleProfile, userId));
        sender.sendToAll(configVersion);
        return views.find(configVersion);
    }

    /** 바꾼 것과 그 바꾸기로 만든 묶음(만들지 않았으면 null) */
    public record Published<T>(T changes, ObjectNode bundle) {
    }

    private record Built<T>(T changes, String configVersion) {
    }

    /**
     * 구역·작업면 바꾸기와 묶음 만들기를 한 트랜잭션으로 한다(S15P31A101-397). 바꾸기나 묶음 만들기(보정 없음 등)가 실패하면
     * 모두 되돌린다. 묶음은 이전 묶음의 규칙 프로필을 이어 쓰고, Tier 0 로 보내는 일은 커밋 뒤에 한다(publish 와 같음).
     * 바꾸기는 트랜잭션 안에서 부르는 저장소 메서드만 써야 한다
     */
    public <T> Published<T> publishWith(Supplier<T> changes, boolean publish, long userId) {
        if (publish && !signer.configured()) {
            throw new BusinessException(ErrorCode.Config.PUBLIC_URL_NOT_SET);
        }
        String siteId = sites.siteId();
        Built<T> built = transaction.execute(status -> {
            T value = changes.get();
            return new Built<>(value, publish ? build(siteId, null, userId) : null);
        });
        if (built.configVersion() == null) {
            return new Published<>(built.changes(), null);
        }
        sender.sendToAll(built.configVersion());
        return new Published<>(built.changes(), views.find(built.configVersion()));
    }

    private String build(String siteId, String requestedProfile, long userId) {
        // 발행을 한 줄로 세운다(버전 번호·이전 버전이 겹치지 않게)
        jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.config_bundle'))", Map.of());
        Map<String, Object> previous = jdbc.queryForList("""
                SELECT config_version, rule_profile FROM config_bundle WHERE site_id = :site ORDER BY issued_at DESC LIMIT 1
                """, new MapSqlParameterSource("site", siteId)).stream().findFirst().orElse(null);
        String previousVersion = previous == null ? null : (String) previous.get("config_version");
        String profile = requestedProfile != null ? requestedProfile
                : previous != null && previous.get("rule_profile") != null ? (String) previous.get("rule_profile") : OPERATION;
        Integer last = jdbc.queryForObject("""
                SELECT max(CAST(substring(config_version FROM 5) AS integer)) FROM config_bundle WHERE config_version ~ '^cfg-[0-9]+$'
                """, Map.of(), Integer.class);
        String configVersion = VERSION_PREFIX + ((last == null ? 0 : last) + 1);
        // 매니페스트와 알림(DB 에서 다시 읽음)의 issued_at 이 같아야 Tier 0 가 받는다. DB 가 µs 로 반올림하므로 ms 로 자른다
        Instant issuedAt = clock.instant().truncatedTo(ChronoUnit.MILLIS);

        Calibration calibration = latestCalibration(siteId);
        List<Item> items = new ArrayList<>();
        ObjectNode zonesDoc = zones(siteId, configVersion + "-zones", items);
        ObjectNode rulesDoc = rules(profile, configVersion + "-rules", items);
        ObjectNode geometryDoc = geometry(siteId, configVersion + "-geometry", items);

        Map<String, byte[]> files = new LinkedHashMap<>();
        files.put("zones", mapper.writeValueAsBytes(zonesDoc));
        files.put("rules", mapper.writeValueAsBytes(rulesDoc));
        files.put("calibration", calibration.content());
        files.put("operational_geometry", mapper.writeValueAsBytes(geometryDoc));

        ObjectNode components = mapper.createObjectNode();
        for (String name : ConfigFiles.COMPONENTS) {
            ObjectNode ref = components.putObject(name);
            ref.put("version", switch (name) {
                case "zones" -> zonesDoc.get("version").asString();
                case "rules" -> rulesDoc.get("version").asString();
                case "calibration" -> calibration.version();
                default -> geometryDoc.get("version").asString();
            });
            ref.put("sha256", ConfigFiles.sha256(files.get(name)));
            ref.put("uri", signer.url(configVersion, name));
            if (name.equals("rules")) {
                ref.set("rule_versions", rulesDoc.get("rule_versions").deepCopy());
            }
        }
        ObjectNode manifest = mapper.createObjectNode();
        manifest.put("config_version", configVersion);
        manifest.put("previous_config_version", previousVersion);
        manifest.put("issued_at", issuedAt.toEpochMilli());
        manifest.set("components", components);
        byte[] manifestBytes = mapper.writeValueAsBytes(manifest);
        files.put(ConfigFiles.MANIFEST, manifestBytes);

        jdbc.update("""
                INSERT INTO config_bundle (config_version, previous_config_version, site_id, issued_at, issued_by, bundle_uri,
                    bundle_hash, components, calibration_version, rule_profile)
                VALUES (:v, :previous, :site, :issuedAt, :user, :uri, :hash, CAST(:components AS jsonb), :calibration, :profile)
                """, new MapSqlParameterSource()
                .addValue("v", configVersion).addValue("previous", previousVersion).addValue("site", siteId)
                .addValue("issuedAt", OffsetDateTime.ofInstant(issuedAt, ZoneOffset.UTC)).addValue("user", userId)
                .addValue("uri", signer.url(configVersion, ConfigFiles.MANIFEST))
                .addValue("hash", ConfigFiles.sha256(manifestBytes)).addValue("components", mapper.writeValueAsString(components))
                .addValue("calibration", calibration.version()).addValue("profile", profile));
        for (Map.Entry<String, byte[]> file : files.entrySet()) {
            jdbc.update("""
                    INSERT INTO config_bundle_file (config_version, name, content, sha256) VALUES (:v, :name, :content, :sha)
                    """, new MapSqlParameterSource().addValue("v", configVersion).addValue("name", file.getKey())
                    .addValue("content", file.getValue()).addValue("sha", ConfigFiles.sha256(file.getValue())));
        }
        jdbc.batchUpdate("""
                INSERT INTO config_bundle_item (config_version, item_type, item_id, item_version) VALUES (:v, :type, :id, :version)
                """, items.stream().map(item -> new MapSqlParameterSource().addValue("v", configVersion)
                .addValue("type", item.type()).addValue("id", item.id()).addValue("version", item.version()))
                .toArray(MapSqlParameterSource[]::new));
        return configVersion;
    }

    private record Item(String type, String id, int version) {
    }

    private record Calibration(String version, byte[] content) {
    }

    /**
     * Tier 0 가 알린 현재 보정 버전의 문서를 싣는다(S15P31A101-263). 그 문서를 아직 올리지 않았으면 묶음을 만들지 않는다
     * (화면의 현재 보정과 묶음의 보정이 갈리지 않게). 알림을 받은 적이 없으면(Tier 0 연결 전) 가장 최근에 올린 문서를 싣는다.
     */
    private Calibration latestCalibration(String siteId) {
        Optional<String> current = calibrations.currentVersion();
        if (current.isPresent()) {
            return jdbc.query("""
                    SELECT calibration_version, content FROM calibration_document WHERE calibration_version = :v
                    """, new MapSqlParameterSource("v", current.get()),
                    (rs, i) -> new Calibration(rs.getString("calibration_version"), rs.getBytes("content")))
                    .stream().findFirst().orElseThrow(() -> new BusinessException(ErrorCode.Config.CURRENT_CALIBRATION_MISSING));
        }
        return jdbc.query("""
                SELECT d.calibration_version, d.content FROM calibration_document d
                JOIN calibration_version v ON v.calibration_version = d.calibration_version
                WHERE v.site_id = :site ORDER BY d.uploaded_at DESC LIMIT 1
                """, new MapSqlParameterSource("site", siteId),
                (rs, i) -> new Calibration(rs.getString("calibration_version"), rs.getBytes("content")))
                .stream().findFirst().orElseThrow(() -> new BusinessException(ErrorCode.Config.CALIBRATION_MISSING));
    }

    /** Tier 0 Zone.from_json 형식. 모르는 키를 거부하므로 그 키만 싣는다 */
    private ObjectNode zones(String siteId, String version, List<Item> items) {
        ObjectNode doc = mapper.createObjectNode();
        doc.put("version", version);
        ArrayNode zones = doc.putArray("zones");
        jdbc.query("""
                SELECT zone_id, zone_version, eval_type, ST_AsGeoJSON(polygon) AS polygon, z_min_m, z_max_m, severity, zone_kind,
                    valid_from
                FROM zone WHERE site_id = :site AND valid_to IS NULL ORDER BY zone_id
                """, new MapSqlParameterSource("site", siteId), rs -> {
            ObjectNode z = zones.addObject();
            z.put("zone_id", rs.getString("zone_id"));
            z.put("zone_version", String.valueOf(rs.getInt("zone_version")));
            z.put("judge_type", rs.getString("eval_type"));
            Versioned.polygonXy(mapper.readTree(rs.getString("polygon")), z.putArray("polygon_xy"));
            Double zMin = Versioned.real(rs, "z_min_m");
            Double zMax = Versioned.real(rs, "z_max_m");
            if (zMin != null && zMax != null) {
                z.putArray("z_range").add(zMin).add(zMax);
            } else {
                z.putNull("z_range");
            }
            z.put("severity", rs.getString("severity"));
            z.put("zone_type", rs.getString("zone_kind"));
            z.put("valid_from", rs.getObject("valid_from", OffsetDateTime.class).toInstant().toEpochMilli());
            items.add(new Item("ZONE", rs.getString("zone_id"), rs.getInt("zone_version")));
        });
        return doc;
    }

    private ObjectNode rules(String profile, String version, List<Item> items) {
        ObjectNode doc = mapper.createObjectNode();
        doc.put("version", version);
        ArrayNode list = doc.putArray("rule_versions");
        // 규칙 ID 마다 하나. 교실 시험이면 그 규칙의 교실 시험 버전을 먼저, 없으면 운영 버전(규칙 조회 GET /api/rules 와 같은 선택)
        jdbc.query(RuleSelection.ACTIVE_FOR_PROFILE, new MapSqlParameterSource("profile", profile), rs -> {
            list.addObject().put("rule_id", rs.getString("rule_id")).put("rule_version", rs.getInt("rule_version"));
            items.add(new Item("RULE", rs.getString("rule_id"), rs.getInt("rule_version")));
        });
        if (list.isEmpty()) {
            throw new BusinessException(ErrorCode.Config.RULES_MISSING);
        }
        return doc;
    }

    /** 작업면은 Tier 0 work_surfaces 형식(OBB 바닥 투영 + 상판 높이), 장애물은 static_objects 에 OBB 그대로 */
    private ObjectNode geometry(String siteId, String version, List<Item> items) {
        ObjectNode doc = mapper.createObjectNode();
        doc.put("version", version);
        ArrayNode surfaces = doc.putArray("work_surfaces");
        ArrayNode objects = doc.putArray("static_objects");
        jdbc.query("""
                SELECT object_id, object_version, kind, obb::text AS obb, top_height_m FROM static_object
                WHERE site_id = :site AND valid_to IS NULL ORDER BY object_id
                """, new MapSqlParameterSource("site", siteId), rs -> {
            JsonNode obb = mapper.readTree(rs.getString("obb"));
            String id = rs.getString("object_id");
            if ("WORK_SURFACE".equals(rs.getString("kind"))) {
                ObjectNode s = surfaces.addObject();
                s.put("surface_id", id);
                s.put("top_height_m", Versioned.real(rs, "top_height_m"));
                s.putArray("center_xy").add(obb.get("center").get(0).doubleValue()).add(obb.get("center").get(1).doubleValue());
                s.putArray("size_xy").add(obb.get("size").get(0).doubleValue()).add(obb.get("size").get(1).doubleValue());
                s.put("yaw_rad", obb.path("yaw_rad").doubleValue());
                if (obb.has("top_height_sigma_m")) {
                    s.put("top_height_sigma_m", obb.get("top_height_sigma_m").doubleValue());
                }
            } else {
                ObjectNode o = objects.addObject();
                o.put("object_id", id);
                o.put("kind", rs.getString("kind"));
                o.set("obb", obb);
                Double top = Versioned.real(rs, "top_height_m");
                if (top != null) {
                    o.put("top_height_m", top);
                }
            }
            items.add(new Item("STATIC_OBJECT", id, rs.getInt("object_version")));
        });
        return doc;
    }
}
