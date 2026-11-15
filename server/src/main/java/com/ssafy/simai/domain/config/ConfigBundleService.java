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
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 구성 묶음 발행(명세 3.2 구성 묶음, 11.1, 계약 config_bundle). 발행 때 지금 유효한 구역·규칙·보정·작업면을 묶어 새 config_version 을
 * 만든다(덮어쓰지 않는다). 묶음 파일(매니페스트와 구성 요소 5개)의 바이트·해시를 저장하고, 커밋 뒤 연결된 Tier 0 에 알린다.
 * Tier 0 는 파일을 서명 주소로 내려받아 해시·형식·참조를 검사하고 원자적으로 전환한 뒤 config_ack 로 답한다. 그 전까지는 '적용 대기'다.
 *
 * <p>구성 요소 내용 형식은 계약 범위 밖이라 Tier 0 의 임시 형식(ai/src/simai_ai/config/bundle.py 표)을 따른다.
 * <ul>
 *   <li>zones: {"version", "zones": [Zone.from_json 형식]}. 지금 유효한 버전만 싣는다(valid_from 은 ms, valid_to 는 없음)
 *   <li>rules: {"version", "rule_versions": [...]}. 규칙 ID 마다 하나: 운영(OPERATION) 또는 교실 시험(CLASSROOM_TEST, 그 규칙에
 *       교실 시험 버전이 없으면 운영 버전)
 *   <li>calibration: 가장 최근에 올린 보정 문서 원문
 *   <li>operational_geometry: {"version", "work_surfaces": [...], "static_objects": [...]}. 작업면은 OBB 바닥 투영과 상판 높이
 *   <li>ppe_policy: 계약 data/ppe_policy 1.2.0(임시 형식 아님). 그때 작업 상태로 만든 안전모·조끼 요구(S15P31A101-518·-549·-654)
 * </ul>
 * 규칙 프로필은 현장 설정(site.rule_profile)을 따른다. 이전 묶음의 프로필을 잇지 않는다(S15P31A101-654,
 * docs/design/S15P31A101-654-ppe-r3-demo.md). 프로필을 지정한 발행은 현장 설정도 그 값으로 바꾼다.
 * 기본은 교실 시험이다(S15P31A101-669, 시연 고정. 현장 행이 없을 때도 교실 시험).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ConfigBundleService {

    public static final String OPERATION = "OPERATION";
    public static final String CLASSROOM_TEST = "CLASSROOM_TEST";
    static final Set<String> RULE_PROFILES = Set.of(OPERATION, CLASSROOM_TEST);
    /** 현장 설정이 없을 때의 규칙 기준. 시연 고정으로 교실 시험(S15P31A101-669, DB 기본값 V67 과 같다) */
    public static final String DEFAULT_RULE_PROFILE = RuleSelection.DEFAULT_PROFILE;
    private static final String VERSION_PREFIX = "cfg-";
    private static final Logger log = LoggerFactory.getLogger(ConfigBundleService.class);

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;
    private final BundleUrlSigner signer;
    private final SiteLookup sites;
    private final ConfigBundleSender sender;
    private final ConfigBundleViews views;
    private final CalibrationViews calibrations;
    private final Clock clock;
    /** 커밋 뒤 발행(세팅 결과)은 끝난 트랜잭션에 끼지 않게 새 트랜잭션으로 한다 */
    private final TransactionTemplate requiresNew;

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
        this.requiresNew = new TransactionTemplate(transaction.getTransactionManager());
        this.requiresNew.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
    }

    /** @param ruleProfile 없으면 현장 설정(site.rule_profile). 있으면 현장 설정을 그 값으로 바꾸고(같은 트랜잭션) 그 값으로 만든다 */
    public ObjectNode publish(String ruleProfile, long userId) {
        if (ruleProfile != null && !RULE_PROFILES.contains(ruleProfile)) {
            throw new BusinessException(ErrorCode.Config.INVALID_RULE_PROFILE);
        }
        if (!signer.configured()) {
            throw new BusinessException(ErrorCode.Config.PUBLIC_URL_NOT_SET);
        }
        String siteId = sites.siteId();
        String configVersion = transaction.execute(status -> build(siteId, ruleProfile, userId, null));
        sender.sendToAll(configVersion);
        return views.find(configVersion);
    }

    /** 현장 규칙 기준 바꾸기 결과. bundle 은 새로 만든 묶음, 만들지 못했으면 null 이고 publishError 에 그 오류 코드 */
    public record RuleProfileChange(String ruleProfile, ObjectNode bundle, String publishError) {
    }

    /**
     * 현장 규칙 기준을 바꾸고 그 기준으로 새 묶음을 발행한다(S15P31A101-654). 설정은 묶음과 따로 커밋한다: 묶음을 만들지 못해도
     * (보정 없음·서명 주소 미설정 등) 설정은 남고, 다음 묶음(세팅 완료·작업 시작·구역 저장)이 그 기준을 따른다.
     */
    public RuleProfileChange changeRuleProfile(String ruleProfile, long userId) {
        if (ruleProfile == null || !RULE_PROFILES.contains(ruleProfile)) {
            throw new BusinessException(ErrorCode.Config.INVALID_RULE_PROFILE);
        }
        String siteId = sites.siteId();
        transaction.executeWithoutResult(status -> saveRuleProfile(siteId, ruleProfile));
        try {
            return new RuleProfileChange(ruleProfile, publish(null, userId), null);
        } catch (BusinessException e) {
            log.warn("규칙 기준 {} 저장, 묶음은 만들지 못함(다음 묶음이 따른다): {}", ruleProfile, e.getErrorCode().name());
            return new RuleProfileChange(ruleProfile, null, e.getErrorCode().name());
        }
    }

    /** 현장 규칙 기준(site.rule_profile). 현장 행이 없으면 교실 시험(DEFAULT_RULE_PROFILE) */
    public String ruleProfile() {
        return siteRuleProfile(sites.siteId());
    }

    private String siteRuleProfile(String siteId) {
        return jdbc.queryForList("SELECT rule_profile FROM site WHERE site_id = :site",
                new MapSqlParameterSource("site", siteId), String.class).stream().findFirst().orElse(DEFAULT_RULE_PROFILE);
    }

    private void saveRuleProfile(String siteId, String ruleProfile) {
        jdbc.update("UPDATE site SET rule_profile = :profile WHERE site_id = :site",
                new MapSqlParameterSource("site", siteId).addValue("profile", ruleProfile));
    }

    /** 바꾼 것과 그 바꾸기로 만든 묶음(만들지 않았으면 null) */
    public record Published<T>(T changes, ObjectNode bundle) {
    }

    private record Built<T>(T changes, String configVersion) {
    }

    /**
     * 구역·작업면 바꾸기와 묶음 만들기를 한 트랜잭션으로 한다(S15P31A101-397). 바꾸기나 묶음 만들기(보정 없음 등)가 실패하면
     * 모두 되돌린다. 묶음은 현장 규칙 기준(site.rule_profile)을 따르고, Tier 0 로 보내는 일은 커밋 뒤에 한다(publish 와 같음).
     * 바꾸기는 트랜잭션 안에서 부르는 저장소 메서드만 써야 한다
     */
    public <T> Published<T> publishWith(Supplier<T> changes, boolean publish, long userId) {
        if (publish && !signer.configured()) {
            throw new BusinessException(ErrorCode.Config.PUBLIC_URL_NOT_SET);
        }
        String siteId = sites.siteId();
        Built<T> built = transaction.execute(status -> {
            T value = changes.get();
            return new Built<>(value, publish ? build(siteId, null, userId, null) : null);
        });
        if (built.configVersion() == null) {
            return new Published<>(built.changes(), null);
        }
        sender.sendToAll(built.configVersion());
        return new Published<>(built.changes(), views.find(built.configVersion()));
    }

    /** 변경 결과에 따라 발행 여부를 트랜잭션 안에서 결정한다. */
    public <T> Published<T> publishWithDynamic(Supplier<T> changes, java.util.function.Predicate<T> publish, long userId) {
        String siteId = sites.siteId();
        Built<T> built = transaction.execute(status -> {
            T value = changes.get();
            if (!publish.test(value)) return new Built<>(value, null);
            if (!signer.configured()) throw new BusinessException(ErrorCode.Config.PUBLIC_URL_NOT_SET);
            return new Built<>(value, build(siteId, null, userId, null));
        });
        if (built.configVersion() == null) return new Published<>(built.changes(), null);
        sender.sendToAll(built.configVersion());
        return new Published<>(built.changes(), views.find(built.configVersion()));
    }

    /**
     * 현장 세팅 성공(I5 site_setup 결과) 때 세팅이 등록한 새 보정을 실은 묶음을 발행한다(S15P31A101-467). Tier 0 가 알린 현재 보정 대신
     * 그 보정 문서를 싣고, 구역·규칙·작업면은 발행과 같이 지금 유효한 것을 싣는다. 규칙 프로필은 현장 설정을 따른다.
     * 호출자 트랜잭션(결과 제출)이 커밋된 뒤 별도 트랜잭션에서 만들고 연결된 Tier 0 에 보낸다(트랜잭션 밖이면 바로). 그래서 묶음을
     * 만들지 못해도(규칙 없음·서명 주소 미설정·보정 문서 없음) 세팅 완료는 되돌아가지 않고, 경고만 남기며 Tier 0 는 이전 묶음 그대로다.
     * 그 보정을 실은 묶음이 이미 있으면 새로 만들지 않는다.
     */
    public void publishForSetupAfterCommit(String calibrationVersion, long userId) {
        Runnable task = () -> {
            try {
                if (!signer.configured()) {
                    throw new BusinessException(ErrorCode.Config.PUBLIC_URL_NOT_SET);
                }
                String siteId = sites.siteId();
                String configVersion = requiresNew.execute(status -> {
                    jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.config_bundle'))", Map.of());
                    boolean exists = !jdbc.queryForList("""
                            SELECT 1 FROM config_bundle WHERE site_id = :site AND calibration_version = :cal LIMIT 1
                            """, new MapSqlParameterSource("site", siteId).addValue("cal", calibrationVersion)).isEmpty();
                    if (exists) {
                        return null;
                    }
                    // 관제 중(IN_PROGRESS) 작업이 있으면 보정·World 를 바꾸지 않는다(세팅은 작업 시작 전 절차)
                    boolean live = !jdbc.queryForList("""
                            SELECT 1 FROM work_unit WHERE site_id = :site AND status = 'IN_PROGRESS' LIMIT 1
                            """, new MapSqlParameterSource("site", siteId)).isEmpty();
                    if (live) {
                        log.warn("세팅 보정 {} 구성 묶음을 발행하지 않는다: 같은 현장에 진행 중(IN_PROGRESS) 작업이 있다", calibrationVersion);
                        return null;
                    }
                    return build(siteId, null, userId, calibrationVersion);
                });
                if (configVersion != null) {
                    log.info("세팅 보정 {} 으로 구성 묶음 {} 발행", calibrationVersion, configVersion);
                    sender.sendToAll(configVersion);
                }
            } catch (RuntimeException e) {
                log.warn("세팅 보정 {} 의 구성 묶음 발행 실패(세팅 완료는 유지, Tier 0 는 이전 묶음 그대로): {}", calibrationVersion,
                        e.getMessage());
            }
        };
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    task.run();
                }
            });
            return;
        }
        task.run();
    }

    /**
     * 세팅 완료(C17, S15P31A101-615): 부르는 쪽 트랜잭션 안에서 묶음을 만든다(구역 적용·완료 기록과 함께 커밋되거나 함께 되돌아간다).
     * calibrationVersion 이 있으면 그 보정 문서를 싣고(세팅이 등록한 새 보정), 없으면 발행과 같이 지금 보정을 싣는다.
     * Tier 0 로 보내는 일은 커밋 뒤에 한다. 묶음을 만들지 못하면(규칙 없음·서명 주소 미설정·보정 문서 없음) 던져 모두 되돌린다.
     */
    public String publishInTransaction(String calibrationVersion, long userId) {
        if (!TransactionSynchronizationManager.isActualTransactionActive()) {
            throw new IllegalStateException("publishInTransaction 은 부르는 쪽 트랜잭션 안에서만 부른다");
        }
        if (!signer.configured()) {
            throw new BusinessException(ErrorCode.Config.PUBLIC_URL_NOT_SET);
        }
        String configVersion = build(sites.siteId(), null, userId, calibrationVersion);
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCommit() {
                try {
                    sender.sendToAll(configVersion);
                } catch (RuntimeException e) {
                    // 묶음은 커밋됐다. 다시 보내기는 미확인 묶음 재전송(ConfigBundleSender)이 맡는다
                    log.warn("구성 묶음 {} 전송 실패(묶음은 저장됨): {}", configVersion, e.getMessage());
                }
            }
        });
        return configVersion;
    }

    private String build(String siteId, String requestedProfile, long userId, String explicitCalibration) {
        // 발행을 한 줄로 세운다(버전 번호·이전 버전이 겹치지 않게)
        jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.config_bundle'))", Map.of());
        String previousVersion = jdbc.queryForList("""
                SELECT config_version FROM config_bundle WHERE site_id = :site ORDER BY issued_at DESC LIMIT 1
                """, new MapSqlParameterSource("site", siteId), String.class).stream().findFirst().orElse(null);
        // 규칙 프로필은 현장 설정이 정한다(S15P31A101-654). 지정한 발행은 현장 설정도 바꿔 이후 묶음이 같은 기준을 쓰게 한다
        if (requestedProfile != null) {
            saveRuleProfile(siteId, requestedProfile);
        }
        String profile = requestedProfile != null ? requestedProfile : siteRuleProfile(siteId);
        Integer last = jdbc.queryForObject("""
                SELECT max(CAST(substring(config_version FROM 5) AS integer)) FROM config_bundle WHERE config_version ~ '^cfg-[0-9]+$'
                """, Map.of(), Integer.class);
        String configVersion = VERSION_PREFIX + ((last == null ? 0 : last) + 1);
        // 매니페스트와 알림(DB 에서 다시 읽음)의 issued_at 이 같아야 Tier 0 가 받는다. DB 가 µs 로 반올림하므로 ms 로 자른다
        Instant issuedAt = clock.instant().truncatedTo(ChronoUnit.MILLIS);

        Calibration calibration = explicitCalibration == null ? latestCalibration(siteId) : documentOf(siteId, explicitCalibration);
        List<Item> items = new ArrayList<>();
        ObjectNode zonesDoc = zones(siteId, configVersion + "-zones", items);
        ObjectNode rulesDoc = rules(profile, configVersion + "-rules", items);
        ObjectNode geometryDoc = geometry(siteId, configVersion + "-geometry", items);
        ObjectNode ppeDoc = ppePolicy(siteId, configVersion + "-ppe", issuedAt);

        Map<String, byte[]> files = new LinkedHashMap<>();
        files.put("zones", mapper.writeValueAsBytes(zonesDoc));
        files.put("rules", mapper.writeValueAsBytes(rulesDoc));
        files.put("calibration", calibration.content());
        files.put("operational_geometry", mapper.writeValueAsBytes(geometryDoc));
        files.put(ConfigFiles.PPE_POLICY, mapper.writeValueAsBytes(ppeDoc));

        ObjectNode components = mapper.createObjectNode();
        for (String name : ConfigFiles.COMPONENTS) {
            ObjectNode ref = components.putObject(name);
            ref.put("version", switch (name) {
                case "zones" -> zonesDoc.get("version").asString();
                case "rules" -> rulesDoc.get("version").asString();
                case "calibration" -> calibration.version();
                case "operational_geometry" -> geometryDoc.get("version").asString();
                case ConfigFiles.PPE_POLICY -> ppeDoc.get("version").asString();
                default -> throw new IllegalStateException("모르는 구성 요소: " + name);
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
        jdbc.update("""
                UPDATE setup_session SET config_version = :v, updated_at = :issuedAt
                WHERE site_id = :site AND calibration_version = :calibration
                  AND status IN ('READY_FOR_ZONES', 'BAKING_TEXTURE', 'COMPLETED')
                """, new MapSqlParameterSource().addValue("v", configVersion).addValue("site", siteId)
                .addValue("calibration", calibration.version())
                .addValue("issuedAt", OffsetDateTime.ofInstant(issuedAt, ZoneOffset.UTC)));
        return configVersion;
    }

    private record Item(String type, String id, int version) {
    }

    private record Calibration(String version, byte[] content) {
    }

    /**
     * Tier 0 가 알린 현재 보정 버전의 문서를 싣는다(S15P31A101-263). 그 문서를 아직 올리지 않았으면 묶음을 만들지 않는다
     * (화면의 현재 보정과 묶음의 보정이 갈리지 않게). 알림을 받은 적이 없으면(Tier 0 연결 전) 가장 최근에 올린 문서를 싣는다.
     * 단, 세팅 보정 묶음을 발행했는데 Tier 0 가 아직 그 보정을 알리지 않았으면(지금 보정이 그 세팅 보정의 이전 버전) 세팅 보정을
     * 싣는다(S15P31A101-467). 굽기 도중 구역을 발행해도 새 World 의 구역과 새 보정이 한 묶음에 들어가게 한다.
     */
    private Calibration latestCalibration(String siteId) {
        Optional<String> current = calibrations.currentVersion();
        Optional<Calibration> setup = pendingSetupCalibration(siteId, current);
        if (setup.isPresent()) {
            return setup.get();
        }
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

    /** 가장 최근 세팅 보정 묶음의 보정이 Tier 0 의 지금 보정에서 출발했고 아직 적용 알림이 없으면 그 보정 문서 */
    private Optional<Calibration> pendingSetupCalibration(String siteId, Optional<String> current) {
        if (current.isEmpty()) {
            return Optional.empty();
        }
        return jdbc.query("""
                SELECT b.calibration_version, v.previous_calibration_version, d.content
                FROM config_bundle b
                JOIN calibration_version v ON v.calibration_version = b.calibration_version
                JOIN calibration_document d ON d.calibration_version = b.calibration_version
                WHERE b.site_id = :site
                  AND (EXISTS (SELECT 1 FROM setup_session_artifact a
                               WHERE a.kind = 'CALIBRATION' AND a.version = b.calibration_version)
                       -- 이전 맵 재사용(S15P31A101-714)이 그 맵의 보정을 Tier 0 지금 보정 뒤 새 버전으로 다시 낸 것
                       OR EXISTS (SELECT 1 FROM setup_session r
                                  WHERE r.calibration_version = b.calibration_version AND r.reuse_source_session_id IS NOT NULL))
                ORDER BY b.issued_at DESC LIMIT 1
                """, new MapSqlParameterSource("site", siteId), (rs, i) -> new Object[] {
                        rs.getString("calibration_version"), rs.getString("previous_calibration_version"), rs.getBytes("content")})
                .stream().findFirst()
                .filter(row -> current.get().equals(row[1]))
                .map(row -> new Calibration((String) row[0], (byte[]) row[2]));
    }

    /** 지정한 보정 버전의 문서(같은 현장). 문서가 없으면 묶음을 만들지 않는다 */
    private Calibration documentOf(String siteId, String version) {
        return jdbc.query("""
                SELECT d.calibration_version, d.content FROM calibration_document d
                JOIN calibration_version v ON v.calibration_version = d.calibration_version
                WHERE d.calibration_version = :v AND v.site_id = :site
                """, new MapSqlParameterSource("v", version).addValue("site", siteId),
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

    /**
     * 안전모·조끼 요구(계약 data/ppe_policy 1.2.0, 명세 7.2 R9·R11·8.10·12.3). 진행 중(IN_PROGRESS) 작업이 있으면 그 작업의 요구
     * (work_unit.helmet_required·vest_required, 기본 필수, S15P31A101-654), 없으면 둘 다 NOT_REQUIRED. vest 는 늘 싣는다(S15P31A101-549).
     * 작업 중 NOT_REQUIRED 는 1.2.0 에서 허용했다. 1.1.0 만 아는 Tier 0 는 그 문서(끈 작업)만 거부하므로 Tier 0 를 먼저 올린다(12.3).
     * 발행 잠금 뒤에 읽어 같은 트랜잭션의 작업 시작·종료와, 먼저 커밋된 다른 트랜잭션의 시작·종료를 본다. 어느 경로로 발행해도 싣는다.
     * effective_from_ms 는 진행 중 작업의 지금 실행 시작 시각(S15P31A101-696, 실행 1이면 작업 시작), 없으면 가장 최근 실제 종료 시각, 끝난 작업도 없으면 발행 시각이다
     * (작업 시작·종료 묶음이면 그 시각과 같다, docs/design/server-ppe-policy-publish.md)
     */
    private ObjectNode ppePolicy(String siteId, String version, Instant issuedAt) {
        MapSqlParameterSource site = new MapSqlParameterSource("site", siteId);
        Map<String, Object> active = jdbc.queryForList("""
                SELECT w.work_id, w.helmet_required, w.vest_required,
                    -- 지금 실행의 시작(S15P31A101-696). 실행 기록이 없으면 작업 시작
                    COALESCE((SELECT max(r.started_at) FROM work_run_window r WHERE r.work_id = w.work_id AND r.ended_at IS NULL),
                        w.actual_started_at) AS actual_started_at
                FROM work_unit w
                WHERE w.site_id = :site AND w.status = 'IN_PROGRESS'
                ORDER BY w.actual_started_at DESC LIMIT 1
                """, site).stream().findFirst().orElse(null);
        ObjectNode doc = mapper.createObjectNode();
        doc.put("format_version", "1.2.0");
        doc.put("version", version);
        if (active != null) {
            doc.put("work_id", ((Number) active.get("work_id")).longValue());
            doc.put("effective_from_ms", millisOf(active.get("actual_started_at")));
            doc.put("helmet", Boolean.FALSE.equals(active.get("helmet_required")) ? "NOT_REQUIRED" : "REQUIRED");
            doc.put("vest", Boolean.FALSE.equals(active.get("vest_required")) ? "NOT_REQUIRED" : "REQUIRED");
            return doc;
        }
        Object lastEnd = jdbc.queryForObject("SELECT max(actual_ended_at) FROM work_unit WHERE site_id = :site", site, Object.class);
        doc.putNull("work_id");
        doc.put("effective_from_ms", lastEnd == null ? issuedAt.toEpochMilli() : millisOf(lastEnd));
        doc.put("helmet", "NOT_REQUIRED");
        doc.put("vest", "NOT_REQUIRED");
        return doc;
    }

    private static long millisOf(Object timestamp) {
        return switch (timestamp) {
            case java.sql.Timestamp t -> t.getTime();
            case OffsetDateTime t -> t.toInstant().toEpochMilli();
            default -> throw new IllegalStateException("시각 형식을 모른다: " + timestamp.getClass());
        };
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
                s.putArray("center_xy").add(obbAxis(obb, "center", 0, id)).add(obbAxis(obb, "center", 1, id));
                s.putArray("size_xy").add(obbAxis(obb, "size", 0, id)).add(obbAxis(obb, "size", 1, id));
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

    /** 저장 때 검사한 obb 의 축 값. 손상된 행은 NPE 대신 어느 작업면인지 밝혀 실패시킨다 */
    private static double obbAxis(JsonNode obb, String field, int axis, String objectId) {
        JsonNode v = obb == null ? null : obb.path(field).path(axis);
        if (v == null || !v.isNumber()) {
            throw new IllegalStateException("static_object " + objectId + " 의 obb." + field + "[" + axis + "] 가 수가 아니다");
        }
        return v.doubleValue();
    }
}
