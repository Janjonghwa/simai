package com.ssafy.simai.domain.gpu;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.properties.GpuProperties;
import java.math.BigDecimal;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * GPU 설정(S15P31A101-702). 저장할 때마다 gpu_settings_version 에 새 버전을 더한다(덮어쓰지 않음, AGENTS 6). 가장 큰 버전이 지금
 * 설정이고 없으면 서버 기본값(GpuProperties.defaults)이다. 비밀 값(API 키·부팅 비밀값)은 받지도 두지도 않는다.
 */
@Service
public class GpuSettingsService {

    /**
     * 지금 설정. versionId 가 null 이면 서버 기본값. AWS(S15P31A101-703)는 gpuTypes 가 인스턴스 종류(예 g6e.2xlarge), regions 가 가용 영역
     * (예 ap-northeast-2a), interruptible 이 스팟이고, 시작 템플릿·서브넷·보안 그룹은 aws* 칸이다(비밀 아님).
     */
    public record GpuSettings(Long versionId, String provider, List<String> gpuTypes, List<String> regions, String cloudType,
            boolean interruptible, BigDecimal maxPricePerHourUsd, int autoStopIdleMinutes, String templateId, Instant createdAt,
            Long createdBy, String awsLaunchTemplateId, List<String> awsSubnetIds, List<String> awsSecurityGroupIds) {

        public GpuSettings {
            awsSubnetIds = awsSubnetIds == null ? List.of() : List.copyOf(awsSubnetIds);
            awsSecurityGroupIds = awsSecurityGroupIds == null ? List.of() : List.copyOf(awsSecurityGroupIds);
        }
    }

    static final Set<String> PROVIDERS = Set.of("RUNPOD", "AWS");
    private static final Set<String> CLOUD_TYPES = Set.of("SECURE", "COMMUNITY");
    /** RunPod GPU 종류 ID 는 표시 이름 꼴(예: NVIDIA H100 80GB HBM3) */
    private static final Pattern GPU_TYPE = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,99}$");
    /** 데이터센터 ID(예: AP-JP-1, ap-northeast-2) */
    private static final Pattern REGION = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9-]{0,39}$");
    private static final Pattern TEMPLATE_ID = Pattern.compile("^[A-Za-z0-9_-]{1,64}$");
    /** AWS 칸(S15P31A101-703): 인스턴스 종류·가용 영역·시작 템플릿·서브넷·보안 그룹 ID 꼴 */
    private static final Pattern AWS_INSTANCE_TYPE = Pattern.compile("^[a-z][a-z0-9-]*\\.[a-z0-9-]+$");
    private static final Pattern AWS_ZONE = Pattern.compile("^[a-z]{2}(-[a-z]+)+-\\d[a-z]$");
    private static final Pattern AWS_LAUNCH_TEMPLATE = Pattern.compile("^lt-[0-9a-f]{8,17}$");
    private static final Pattern AWS_SUBNET = Pattern.compile("^subnet-[0-9a-f]{8,17}$");
    private static final Pattern AWS_SECURITY_GROUP = Pattern.compile("^sg-[0-9a-f]{8,17}$");
    private static final int MAX_SUBNETS = 10;
    private static final int MAX_SECURITY_GROUPS = 5;
    private static final int MAX_GPU_TYPES = 10;
    private static final int MAX_REGIONS = 20;
    private static final BigDecimal MAX_PRICE = BigDecimal.valueOf(100);

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final GpuProperties.Defaults defaults;

    public GpuSettingsService(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, Clock clock, GpuProperties properties) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.clock = clock;
        this.defaults = properties.defaults();
    }

    /** 지금 설정(가장 최근 버전, 없으면 기본값) */
    public GpuSettings current() {
        List<GpuSettings> rows = jdbc.query("""
                SELECT version_id, provider, preferred_gpu_types::text AS gpu_types, preferred_regions::text AS regions, cloud_type,
                    interruptible, max_price_per_hour_usd, auto_stop_idle_minutes, runpod_template_id, created_at, created_by,
                    aws_launch_template_id, aws_subnet_ids::text AS aws_subnets, aws_security_group_ids::text AS aws_groups
                FROM gpu_settings_version ORDER BY version_id DESC LIMIT 1
                """, Map.of(), (rs, i) -> new GpuSettings(rs.getLong("version_id"), rs.getString("provider"),
                strings(mapper.readTree(rs.getString("gpu_types"))), strings(mapper.readTree(rs.getString("regions"))),
                rs.getString("cloud_type"), rs.getBoolean("interruptible"), rs.getBigDecimal("max_price_per_hour_usd"),
                rs.getInt("auto_stop_idle_minutes"), rs.getString("runpod_template_id"),
                rs.getObject("created_at", OffsetDateTime.class).toInstant(), (Long) rs.getObject("created_by"),
                rs.getString("aws_launch_template_id"), strings(mapper.readTree(rs.getString("aws_subnets"))),
                strings(mapper.readTree(rs.getString("aws_groups")))));
        if (!rows.isEmpty()) {
            return rows.getFirst();
        }
        return new GpuSettings(null, "RUNPOD", defaults.gpuTypes(), defaults.regions(), defaults.cloudType(), defaults.interruptible(),
                defaults.maxPricePerHourUsd() == null ? null : BigDecimal.valueOf(defaults.maxPricePerHourUsd()),
                defaults.autoStopIdleMinutes(), defaults.templateId(), null, null, null, List.of(), List.of());
    }

    /** 새 버전으로 저장한다. 값이 틀리면 INVALID_GPU_SETTINGS */
    public GpuSettings save(JsonNode body, Long userId) {
        if (body == null || !body.isObject()) {
            throw invalid();
        }
        String provider = requiredText(body, "provider");
        if (!PROVIDERS.contains(provider)) {
            throw invalid();
        }
        List<String> gpuTypes = list(body.get("preferred_gpu_types"), GPU_TYPE, MAX_GPU_TYPES);
        List<String> regions = list(body.get("preferred_regions"), REGION, MAX_REGIONS);
        String cloudType = requiredText(body, "cloud_type");
        if (!CLOUD_TYPES.contains(cloudType)) {
            throw invalid();
        }
        JsonNode interruptible = body.get("interruptible");
        if (interruptible == null || !interruptible.isBoolean()) {
            throw invalid();
        }
        BigDecimal maxPrice = null;
        JsonNode price = body.get("max_price_per_hour_usd");
        if (price != null && !price.isNull()) {
            if (!price.isNumber() || price.decimalValue().signum() <= 0 || price.decimalValue().compareTo(MAX_PRICE) > 0) {
                throw invalid();
            }
            maxPrice = price.decimalValue().setScale(4, java.math.RoundingMode.HALF_UP);
        }
        JsonNode minutes = body.get("auto_stop_idle_minutes");
        if (minutes == null || !minutes.isIntegralNumber() || minutes.intValue() < 0 || minutes.intValue() > 1440) {
            throw invalid();
        }
        String templateId = null;
        JsonNode template = body.get("runpod_template_id");
        if (template != null && !template.isNull()) {
            if (!template.isString()) {
                throw invalid();
            }
            String value = template.stringValue().strip();
            if (!value.isEmpty()) {
                if (!TEMPLATE_ID.matcher(value).matches()) {
                    throw invalid();
                }
                templateId = value;
            }
        }
        // AWS 칸(S15P31A101-703, 비밀 아님). 없거나 null 이면 비움. 공급자가 AWS 면 GPU 칸은 인스턴스 종류, 지역 칸은 가용 영역이어야 한다
        String awsTemplate = optionalId(body.get("aws_launch_template_id"), AWS_LAUNCH_TEMPLATE);
        List<String> awsSubnets = optionalList(body.get("aws_subnet_ids"), AWS_SUBNET, MAX_SUBNETS);
        List<String> awsGroups = optionalList(body.get("aws_security_group_ids"), AWS_SECURITY_GROUP, MAX_SECURITY_GROUPS);
        if ("AWS".equals(provider) && (gpuTypes.stream().anyMatch(t -> !AWS_INSTANCE_TYPE.matcher(t).matches())
                || regions.stream().anyMatch(r -> !AWS_ZONE.matcher(r).matches()))) {
            throw invalid();
        }
        Instant now = clock.instant().truncatedTo(ChronoUnit.MILLIS);
        Long versionId = jdbc.queryForObject("""
                INSERT INTO gpu_settings_version (provider, preferred_gpu_types, preferred_regions, cloud_type, interruptible,
                    max_price_per_hour_usd, auto_stop_idle_minutes, runpod_template_id, created_at, created_by,
                    aws_launch_template_id, aws_subnet_ids, aws_security_group_ids)
                VALUES (:provider, CAST(:gpuTypes AS jsonb), CAST(:regions AS jsonb), :cloud, :interruptible, :price, :minutes, :template,
                    :now, :user, :awsTemplate, CAST(:awsSubnets AS jsonb), CAST(:awsGroups AS jsonb))
                RETURNING version_id
                """, new MapSqlParameterSource("provider", provider)
                .addValue("gpuTypes", mapper.writeValueAsString(gpuTypes)).addValue("regions", mapper.writeValueAsString(regions))
                .addValue("cloud", cloudType).addValue("interruptible", interruptible.booleanValue()).addValue("price", maxPrice)
                .addValue("minutes", minutes.intValue()).addValue("template", templateId)
                .addValue("now", OffsetDateTime.ofInstant(now, ZoneOffset.UTC)).addValue("user", userId)
                .addValue("awsTemplate", awsTemplate).addValue("awsSubnets", mapper.writeValueAsString(awsSubnets))
                .addValue("awsGroups", mapper.writeValueAsString(awsGroups)), Long.class);
        return new GpuSettings(versionId, provider, gpuTypes, regions, cloudType, interruptible.booleanValue(), maxPrice,
                minutes.intValue(), templateId, now, userId, awsTemplate, awsSubnets, awsGroups);
    }

    /** 화면 응답 */
    public ObjectNode view(GpuSettings s) {
        ObjectNode node = mapper.createObjectNode();
        if (s.versionId() == null) {
            node.putNull("version_id");
        } else {
            node.put("version_id", s.versionId());
        }
        node.put("provider", s.provider());
        ArrayNode gpuTypes = node.putArray("preferred_gpu_types");
        s.gpuTypes().forEach(gpuTypes::add);
        ArrayNode regions = node.putArray("preferred_regions");
        s.regions().forEach(regions::add);
        node.put("cloud_type", s.cloudType());
        node.put("interruptible", s.interruptible());
        node.put("max_price_per_hour_usd", s.maxPricePerHourUsd());
        node.put("auto_stop_idle_minutes", s.autoStopIdleMinutes());
        node.put("runpod_template_id", s.templateId());
        node.put("aws_launch_template_id", s.awsLaunchTemplateId());
        ArrayNode subnets = node.putArray("aws_subnet_ids");
        s.awsSubnetIds().forEach(subnets::add);
        ArrayNode groups = node.putArray("aws_security_group_ids");
        s.awsSecurityGroupIds().forEach(groups::add);
        if (s.createdAt() == null) {
            node.putNull("created_at_ms");
        } else {
            node.put("created_at_ms", s.createdAt().toEpochMilli());
        }
        return node;
    }

    private static List<String> list(JsonNode node, Pattern pattern, int max) {
        if (node == null || !node.isArray() || node.isEmpty() || node.size() > max) {
            throw invalid();
        }
        Set<String> values = new LinkedHashSet<>();
        for (JsonNode item : node) {
            if (!item.isString()) {
                throw invalid();
            }
            String value = item.stringValue().strip();
            if (!pattern.matcher(value).matches() || !values.add(value)) {
                throw invalid();
            }
        }
        return List.copyOf(values);
    }

    /** 비거나 null 이면 null, 아니면 형식 확인 */
    private static String optionalId(JsonNode node, Pattern pattern) {
        if (node == null || node.isNull()) {
            return null;
        }
        if (!node.isString()) {
            throw invalid();
        }
        String value = node.stringValue().strip();
        if (value.isEmpty()) {
            return null;
        }
        if (!pattern.matcher(value).matches()) {
            throw invalid();
        }
        return value;
    }

    /** 없거나 null 이면 빈 목록, 아니면 형식·개수·중복 확인 */
    private static List<String> optionalList(JsonNode node, Pattern pattern, int max) {
        if (node == null || node.isNull() || node.isArray() && node.isEmpty()) {
            return List.of();
        }
        return list(node, pattern, max);
    }

    private static List<String> strings(JsonNode array) {
        List<String> values = new ArrayList<>();
        if (array != null && array.isArray()) {
            for (JsonNode item : array) {
                if (item.isString()) {
                    values.add(item.stringValue());
                }
            }
        }
        return List.copyOf(values);
    }

    private static String requiredText(JsonNode body, String field) {
        JsonNode node = body.get(field);
        if (node == null || !node.isString()) {
            throw invalid();
        }
        return node.stringValue().strip();
    }

    private static BusinessException invalid() {
        return new BusinessException(ErrorCode.Gpu.INVALID_GPU_SETTINGS);
    }
}
