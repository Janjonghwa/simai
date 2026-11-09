package com.ssafy.simai.domain.report;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.report.RiskEngineReportClient.EngineCallException;
import com.ssafy.simai.domain.report.TbmReportRepository.ActiveRules;
import com.ssafy.simai.domain.report.TbmReportRepository.GeneratingStart;
import com.ssafy.simai.domain.report.TbmReportRepository.RuleRow;
import com.ssafy.simai.domain.report.TbmReportRepository.ZoneMeta;
import com.ssafy.simai.domain.work.WorkTypes;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.net.http.HttpTimeoutException;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.DisposableBean;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * TBM 작업 단위 보고서 생성·조회 서비스(S15P31A101-418, .agy/tbm-interface.md A·B절).
 *
 * <p>실행 순서:
 * <ol>
 *   <li>작업의 연결 레이아웃 현재 revision 조회</li>
 *   <li>Risk Engine {@code POST /v1/plan/evaluate} 호출(계약 {@code risk_plan_request/result}, 출처 {@code PLANNED})</li>
 *   <li>작업 구역(들)의 최근 7일 LIVE {@code risk_event} 이력 집계</li>
 *   <li>Risk Engine {@code POST /v1/report/generate} 호출(개인정보·작업 메타 제외)</li>
 *   <li>{@code report} 행을 {@code SUCCEEDED} 또는 {@code FAILED} 로 완료 기록</li>
 * </ol>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class TbmReportService implements DisposableBean {

    private static final Logger log = LoggerFactory.getLogger(TbmReportService.class);
    private final TbmReportRepository reports;
    private final WorkWrapUpRepository wrapUps;
    private final SiteLookup sites;
    private final TransactionTemplate transaction;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final RiskEngineReportClient engine;
    private final ExecutorService executor;

    public TbmReportService(
            TbmReportRepository reports,
            WorkWrapUpRepository wrapUps,
            SiteLookup sites,
            TransactionTemplate transaction,
            ObjectMapper mapper,
            Clock clock,
            RiskEngineReportClient engine) {
        this.reports = reports;
        this.wrapUps = wrapUps;
        this.sites = sites;
        this.transaction = transaction;
        this.mapper = mapper;
        this.clock = clock;
        this.engine = engine;
        this.executor = Executors.newVirtualThreadPerTaskExecutor();
    }

    /** 서버 기동 시 이전 실행에서 GENERATING 으로 남은 행을 FAILED(INTERRUPTED)로 복구한다. */
    @EventListener(ApplicationReadyEvent.class)
    public int recoverInterrupted() {
        int count = reports.failInterrupted(clock.instant());
        if (count > 0) {
            log.info("Recovered {} interrupted TBM report(s) on startup", count);
        }
        return count;
    }

    /** 시험에서 가짜 Risk Engine 주소·타임아웃을 바꿀 때 쓴다(일일 일지와 같은 호출부를 쓴다). */
    public void configureRiskEngine(String baseUrl, Duration timeout) {
        engine.configure(baseUrl, timeout);
    }

    public ObjectNode start(TbmReportRequest request) {
        if (request == null || request.workId() == null || request.workId() < 1) {
            throw new BusinessException(ErrorCode.Report.INVALID_REPORT_REQUEST);
        }
        String siteId = sites.siteId();
        GeneratingStart started = transaction.execute(
                s -> reports.createGenerating(siteId, request.workId(), clock.instant()));
        executor.submit(() -> runGeneration(siteId, started));
        ObjectNode accepted = mapper.createObjectNode();
        accepted.put("report_id", started.reportId());
        accepted.put("work_id", started.workId());
        accepted.put("report_version", started.reportVersion());
        accepted.put("status", "GENERATING");
        return accepted;
    }

    public ObjectNode getOne(long reportId) {
        return reports.findOne(sites.siteId(), reportId)
                .orElseThrow(() -> new BusinessException(ErrorCode.Report.REPORT_NOT_FOUND));
    }

    /** 종료 트랜잭션 커밋 후에만 호출한다. 네트워크 오류도 집계 사실의 결정론 요약으로 마감한다. */
    public void startWrapUp(long reportId, ObjectNode request) {
        executor.submit(() -> {
            String text;
            String model = "deterministic";
            boolean fallback = true;
            try {
                JsonNode response = engine.postJson("/v1/report/wrap-up", request);
                if (!response.path("text").isString() || response.path("text").asString().isBlank()
                        || !response.path("fallback").isBoolean()) {
                    throw new IllegalArgumentException("Invalid wrap-up response");
                }
                text = response.path("text").asString();
                model = response.path("model").asString("deterministic");
                fallback = response.path("fallback").asBoolean();
            } catch (Exception e) {
                text = wrapUpFallback(request.path("facts"));
                log.warn("Using deterministic wrap-up for report {}", reportId);
            }
            wrapUps.complete(reportId, text, fallback, model, clock.instant());
        });
    }

    static String wrapUpFallback(JsonNode facts) {
        StringBuilder text = new StringBuilder("전체 이벤트 ").append(facts.path("event_count").asLong()).append("건. ");
        for (String severity : List.of("CRITICAL", "HIGH", "CAUTION", "INFO")) {
            text.append("등급 ").append(severity).append(' ').append(facts.path("severity_counts").path(severity).asLong()).append("건. ");
        }
        // risk-engine wrap_up.RULE_DESCRIPTIONS 와 같은 사전(명세 7장 R1~R10, R11 조끼 -549)
        var descriptions = java.util.Map.ofEntries(java.util.Map.entry("R1", "위험 구역 진입"),
                java.util.Map.entry("R2", "가장자리 근접"), java.util.Map.entry("R3", "고소 위치"),
                java.util.Map.entry("R4", "낙상 의심"), java.util.Map.entry("R5", "진입 예측"),
                java.util.Map.entry("R6", "위험 구역 내 관측 상실"), java.util.Map.entry("R7", "구역 감시 능력 저하"),
                java.util.Map.entry("R8", "장비 근접"), java.util.Map.entry("R9", "안전모 미착용"),
                java.util.Map.entry("R10", "불확실 점유 근접"), java.util.Map.entry("R11", "조끼 미착용"));
        for (var entry : facts.path("rule_counts").properties()) {
            text.append("규칙 ").append(entry.getKey()).append(' ').append(descriptions.getOrDefault(entry.getKey(), ""))
                    .append(' ').append(entry.getValue().asLong()).append("건. ");
        }
        for (var zone : facts.path("top_zones")) {
            text.append("구역 ").append(zone.path("name").asString()).append(" 이벤트 ")
                    .append(zone.path("count").asLong()).append("건. ");
        }
        text.append("최고 등급 ").append(facts.path("max_severity").asString("없음")).append(". ");
        text.append("운영자 확인 ").append(facts.path("operator_ack_count").asLong()).append("건. ");
        text.append("운영자 미확인 CRITICAL ").append(facts.path("unacknowledged_critical_count").asLong()).append("건. ");
        text.append("운영자 종료 ").append(facts.path("operator_close_count").asLong()).append("건. ");
        text.append("작업 외 진입 ").append(facts.path("outside_work_entry_count").asLong()).append("건.");
        for (var phase : java.util.Map.of("early", "초반", "middle", "중반", "late", "후반").entrySet()) {
            text.append(" 작업 ").append(phase.getValue()).append(" 이벤트 ")
                    .append(facts.path("phase_counts").path(phase.getKey()).asLong()).append("건.");
        }
        if (!facts.path("first_event_min").isNull() && !facts.path("first_event_min").isMissingNode()) {
            text.append(" 첫 이벤트 시작 후 ").append(facts.path("first_event_min").asLong()).append("분.");
            text.append(" 마지막 이벤트 시작 후 ").append(facts.path("last_event_min").asLong()).append("분.");
        }
        return text.toString();
    }

    public ObjectNode getByWork(long workId) {
        if (workId < 1) {
            throw new BusinessException(ErrorCode.Report.INVALID_REPORT_REQUEST);
        }
        return reports.findByWork(sites.siteId(), workId);
    }

    private void runGeneration(String siteId, GeneratingStart start) {
        ActiveRules activeRules = null;
        JsonNode riskItems = mapper.createArrayNode();
        ArrayNode zoneHistory = mapper.createArrayNode();
        try {
            ArrayNode layoutItems = reports.findLayoutItems(siteId, start.layoutId(), start.layoutRevision());
            activeRules = reports.findActiveRules(siteId);
            ObjectNode planReq = buildPlanRequest(siteId, start, layoutItems, activeRules);

            JsonNode planRes;
            try {
                planRes = engine.postJson("/v1/plan/evaluate", planReq);
            } catch (HttpTimeoutException e) {
                fail(start.reportId(), "ENGINE_TIMEOUT", activeRules.representativeRuleVersion(), riskItems, zoneHistory);
                return;
            } catch (EngineCallException e) {
                fail(start.reportId(), "ENGINE_UNAVAILABLE", activeRules.representativeRuleVersion(), riskItems, zoneHistory);
                return;
            }

            JsonNode payload = planRes.path("payload");
            if (!"OK".equals(payload.path("status").asString(""))) {
                fail(start.reportId(), "ENGINE_UNAVAILABLE", activeRules.representativeRuleVersion(), riskItems, zoneHistory);
                return;
            }
            if (payload.path("items").isArray()) {
                riskItems = payload.path("items");
            }

            ArrayNode zonesNode = buildZones(siteId, start.zoneId(), layoutItems);
            Set<String> historyZoneIds = collectZoneIds(start.zoneId(), zonesNode, riskItems);
            Instant now = clock.instant();
            zoneHistory = reports.aggregateZoneHistory(historyZoneIds, now.minus(Duration.ofDays(7)), now);
            ArrayNode legalBasis = buildLegalBasis(activeRules, start.workType(), riskItems);

            ObjectNode generateReq = buildReportGenerateRequest(
                    start, activeRules.representativeRuleVersion(), riskItems, zonesNode, zoneHistory, legalBasis);

            JsonNode genRes;
            try {
                genRes = engine.postJson("/v1/report/generate", generateReq);
            } catch (HttpTimeoutException e) {
                fail(start.reportId(), "ENGINE_TIMEOUT", activeRules.representativeRuleVersion(), riskItems, zoneHistory);
                return;
            } catch (EngineCallException e) {
                fail(start.reportId(), "ENGINE_UNAVAILABLE", activeRules.representativeRuleVersion(), riskItems, zoneHistory);
                return;
            }

            String genStatus = genRes.path("status").asString("");
            String llmModel = textOrNull(genRes.path("llm").path("model"));
            JsonNode subgraph = genRes.path("subgraph").isObject() ? genRes.path("subgraph") : null;
            JsonNode steps = genRes.path("steps").isArray() ? genRes.path("steps") : null;

            if ("SUCCEEDED".equals(genStatus)) {
                JsonNode sections = genRes.path("sections").isArray() ? genRes.path("sections") : mapper.createArrayNode();
                JsonNode citations = genRes.path("citations").isArray() ? genRes.path("citations") : mapper.createArrayNode();
                reports.completeSucceeded(
                        start.reportId(),
                        activeRules.representativeRuleVersion(),
                        riskItems,
                        zoneHistory,
                        sections,
                        citations,
                        subgraph,
                        steps,
                        llmModel,
                        clock.instant());
            } else {
                String reason = textOrNull(genRes.path("failure_reason"));
                reports.completeFailed(
                        start.reportId(),
                        reason != null ? reason : "ENGINE_UNAVAILABLE",
                        activeRules.representativeRuleVersion(),
                        riskItems,
                        zoneHistory,
                        subgraph,
                        steps,
                        llmModel,
                        clock.instant());
            }
        } catch (Exception e) {
            log.warn("Unexpected error while generating TBM report {}", start.reportId(), e);
            Long ruleVer = activeRules != null ? activeRules.representativeRuleVersion() : null;
            fail(start.reportId(), "ENGINE_UNAVAILABLE", ruleVer, riskItems, zoneHistory);
        }
    }

    private void fail(long reportId, String reason, Long ruleVersion, JsonNode riskItems, JsonNode zoneHistory) {
        reports.completeFailed(reportId, reason, ruleVersion, riskItems, zoneHistory, null, null, null, clock.instant());
    }

    /**
     * layout_plan 항목(OPENING·EQUIPMENT·SCAFFOLD)을 계약 {@code risk_plan_request}(1.0.0) 메시지로 변환한다.
     */
    ObjectNode buildPlanRequest(String siteId, GeneratingStart start, ArrayNode layoutItems, ActiveRules activeRules) {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "risk_plan_request");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", clock.instant().toEpochMilli());
        root.put("source_id", "ec2");

        ObjectNode payload = root.putObject("payload");
        payload.put("request_id", UUID.randomUUID().toString());
        payload.put("source", "PLANNED");
        payload.put("evaluation_time", start.evaluationTime().toEpochMilli());
        if (activeRules.baseConfigVersion() == null) {
            payload.putNull("base_config_version");
        } else {
            payload.put("base_config_version", activeRules.baseConfigVersion());
        }

        ObjectNode rules = payload.putObject("rules");
        rules.put("rules_version", "rules-v" + activeRules.representativeRuleVersion());
        ArrayNode ruleVersions = rules.putArray("rule_versions");
        if (activeRules.rules().isEmpty()) {
            ObjectNode fallback = ruleVersions.addObject();
            fallback.put("rule_id", "R1");
            fallback.put("rule_version", 1);
        } else {
            for (RuleRow r : activeRules.rules()) {
                ObjectNode rv = ruleVersions.addObject();
                rv.put("rule_id", r.ruleId());
                rv.put("rule_version", r.ruleVersion());
            }
        }

        ObjectNode layout = payload.putObject("layout");
        layout.put("layout_id", String.valueOf(start.layoutId()));
        String layoutVersion = "lv-" + start.layoutRevision();
        layout.put("layout_version", layoutVersion);

        ObjectNode frame = layout.putObject("coordinate_frame");
        frame.put("frame", "WORLD");
        frame.put("unit", "m");
        frame.put("handedness", "right");
        frame.put("up_axis", "+Z");
        frame.put("site_id", siteId);

        ArrayNode zones = layout.putArray("zones");
        ArrayNode surfaces = layout.putArray("work_surfaces");

        for (JsonNode item : layoutItems) {
            String itemId = item.path("item_id").asString("");
            String kind = item.path("kind").asString("");
            JsonNode center = item.path("center");
            JsonNode size = item.path("size");
            if (itemId.isEmpty() || center.size() < 3 || size.size() < 3) {
                continue;
            }
            double cx = center.get(0).asDouble();
            double cy = center.get(1).asDouble();
            double cz = center.get(2).asDouble();
            double sx = Math.max(0.01, size.get(0).asDouble());
            double sy = Math.max(0.01, size.get(1).asDouble());
            double sz = Math.max(0.01, size.get(2).asDouble());
            double yaw = item.path("yaw_rad").asDouble(0.0);

            if ("OPENING".equals(kind)) {
                ObjectNode z = zones.addObject();
                z.put("zone_id", itemId);
                z.put("zone_version", layoutVersion);
                z.put("judge_type", "GROUND_FOOTPRINT");
                addObbPolygon(z.putArray("polygon_xy"), cx, cy, sx, sy, yaw);
                z.putNull("z_range");
                z.put("severity", "CRITICAL");
                z.put("zone_type", "OPENING");
            } else if ("EQUIPMENT".equals(kind)) {
                ObjectNode z = zones.addObject();
                z.put("zone_id", itemId);
                z.put("zone_version", layoutVersion);
                z.put("judge_type", "VOLUME_3D");
                addObbPolygon(z.putArray("polygon_xy"), cx, cy, sx, sy, yaw);
                double zMin = round4(Math.max(0.0, cz - sz / 2.0));
                double zMax = round4(Math.max(zMin + 0.1, cz + sz / 2.0));
                z.putArray("z_range").add(zMin).add(zMax);
                z.put("severity", "HIGH");
                z.put("zone_type", "EQUIPMENT_PARKING");
            } else if ("SCAFFOLD".equals(kind) || "WORK_SURFACE".equals(kind)) {
                ObjectNode s = surfaces.addObject();
                s.put("surface_id", "floor".equals(itemId) ? "surface-floor" : itemId);
                double topHeight = cz + sz / 2.0;
                s.put("top_height_m", round4(topHeight > 0 ? topHeight : sz));
                s.putArray("center_xy").add(round4(cx)).add(round4(cy));
                s.putArray("size_xy").add(round4(sx)).add(round4(sy));
                s.put("yaw_rad", round4(yaw));
                s.put("top_height_sigma_m", 0.02);
            }
        }
        return root;
    }

    /**
     * 인터페이스 A({@code POST /v1/report/generate}) 요청 본문을 만든다.
     * 작업명(name), 인원(planned_people), 조 이름(lead), 장소 자유 텍스트(place), 사람 이름은 절대 넣지 않는다.
     */
    ObjectNode buildReportGenerateRequest(
            GeneratingStart start,
            long ruleVersion,
            JsonNode riskItems,
            ArrayNode zones,
            ArrayNode zoneHistory,
            ArrayNode legalBasis) {
        ObjectNode req = mapper.createObjectNode();
        req.put("report_kind", "TBM");
        req.put("request_id", "rpt-" + start.reportId());
        req.put("rule_version", ruleVersion);
        if (start.workType() == null) {
            req.putNull("work_type");
        } else {
            ObjectNode wt = req.putObject("work_type");
            wt.put("code", start.workType());
            wt.put("label", WorkTypes.find(start.workType()).map(WorkTypes.WorkType::label).orElse(start.workType()));
        }
        req.set("risk_items", riskItems);
        req.set("zones", zones);
        req.set("zone_history", zoneHistory);
        req.set("legal_basis", legalBasis);
        return req;
    }

    private ArrayNode buildZones(String siteId, String workZoneId, ArrayNode layoutItems) {
        Map<String, ZoneMeta> byId = new LinkedHashMap<>();
        Optional<ZoneMeta> workZone = reports.findWorkZone(siteId, workZoneId);
        if (workZone.isPresent()) {
            ZoneMeta zm = workZone.get();
            byId.put(zm.zoneId(), zm);
        } else if (workZoneId != null && !workZoneId.isBlank()) {
            byId.put(workZoneId, new ZoneMeta(workZoneId, "GENERAL", workZoneId));
        }
        for (JsonNode item : layoutItems) {
            String kind = item.path("kind").asString("");
            if (!"OPENING".equals(kind) && !"EQUIPMENT".equals(kind)) {
                continue;
            }
            String id = item.path("item_id").asString("");
            if (id.isEmpty() || byId.containsKey(id)) {
                continue;
            }
            String label = textOrNull(item.path("label"));
            byId.put(id, new ZoneMeta(id, kind, label != null ? label : id));
        }
        ArrayNode arr = mapper.createArrayNode();
        for (ZoneMeta zm : byId.values()) {
            ObjectNode z = arr.addObject();
            z.put("zone_id", zm.zoneId());
            z.put("zone_kind", zm.zoneKind());
            z.put("label", zm.label());
        }
        return arr;
    }

    private static Set<String> collectZoneIds(String workZoneId, ArrayNode zones, JsonNode riskItems) {
        Set<String> ids = new LinkedHashSet<>();
        if (workZoneId != null && !workZoneId.isBlank()) {
            ids.add(workZoneId);
        }
        for (JsonNode z : zones) {
            String id = textOrNull(z.path("zone_id"));
            if (id != null) {
                ids.add(id);
            }
        }
        if (riskItems != null && riskItems.isArray()) {
            for (JsonNode item : riskItems) {
                String id = textOrNull(item.path("zone_id"));
                if (id != null) {
                    ids.add(id);
                }
            }
        }
        return ids;
    }

    private ArrayNode buildLegalBasis(ActiveRules activeRules, String workTypeCode, JsonNode riskItems) {
        Set<String> triggeredRules = new LinkedHashSet<>();
        if (riskItems != null && riskItems.isArray()) {
            for (JsonNode item : riskItems) {
                String rId = textOrNull(item.path("rule_id"));
                if (rId != null) {
                    triggeredRules.add(rId);
                }
            }
        }
        Set<String> targetRules = !triggeredRules.isEmpty()
                ? triggeredRules
                : new LinkedHashSet<>(WorkTypes.find(workTypeCode).map(WorkTypes.WorkType::ruleTags).orElse(List.of()));

        Set<String> bases = new LinkedHashSet<>();
        for (RuleRow r : activeRules.rules()) {
            if (!targetRules.isEmpty() && !targetRules.contains(r.ruleId())) {
                continue;
            }
            addSplitBasis(bases, r.legalBasis());
        }
        if (bases.isEmpty()) {
            for (RuleRow r : activeRules.rules()) {
                addSplitBasis(bases, r.legalBasis());
            }
        }
        ArrayNode out = mapper.createArrayNode();
        bases.forEach(out::add);
        return out;
    }

    /** rule_version.legal_basis 의 ";" 로 나뉜 조문들을 더한다(일일 일지도 쓴다). */
    static void addSplitBasis(Set<String> target, String raw) {
        if (raw == null || raw.isBlank()) {
            return;
        }
        for (String part : raw.split(";")) {
            String trimmed = part.strip();
            if (!trimmed.isEmpty()) {
                target.add(trimmed);
            }
        }
    }

    private static void addObbPolygon(ArrayNode polygon, double cx, double cy, double sx, double sy, double yaw) {
        double hx = sx / 2.0;
        double hy = sy / 2.0;
        double cos = Math.cos(yaw);
        double sin = Math.sin(yaw);
        double[][] local = {{-hx, -hy}, {hx, -hy}, {hx, hy}, {-hx, hy}};
        for (double[] corner : local) {
            double wx = round4(cx + corner[0] * cos - corner[1] * sin);
            double wy = round4(cy + corner[0] * sin + corner[1] * cos);
            polygon.addArray().add(wx).add(wy);
        }
    }

    private static double round4(double v) {
        return Math.round(v * 10_000.0) / 10_000.0;
    }

    private static String textOrNull(JsonNode node) {
        if (node == null || node.isNull() || node.isMissingNode()) {
            return null;
        }
        String s = node.asString("").strip();
        return s.isEmpty() ? null : s;
    }

    @Override
    public void destroy() {
        executor.shutdown();
        try {
            executor.awaitTermination(5, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
