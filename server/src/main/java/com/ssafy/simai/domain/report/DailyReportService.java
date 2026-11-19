package com.ssafy.simai.domain.report;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.report.DailyReportRepository.DailyStart;
import com.ssafy.simai.domain.report.DailyReportRepository.EventFact;
import com.ssafy.simai.domain.report.RiskEngineReportClient.EngineCallException;
import com.ssafy.simai.domain.report.TbmReportRepository.ActiveRules;
import com.ssafy.simai.domain.report.TbmReportRepository.RuleRow;
import com.ssafy.simai.domain.report.TbmReportRepository.ZoneMeta;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.net.http.HttpTimeoutException;
import java.time.Clock;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.format.DateTimeParseException;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.DisposableBean;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 일일 안전 일지 생성·조회(S15P31A101-33, FR-122, 기획서 8.3·12.6, docs/design/daily-report.md).
 *
 * <p>그날(Asia/Seoul) LIVE {@code risk_event} 를 모아 Risk Engine {@code POST /v1/report/generate}(report_kind DAILY)에 넘기고,
 * 컨테이너가 법령 그래프 하이브리드 검색·외부 LLM·코드 4단계 검증으로 만든 본문을 TBM 과 같은 {@code report} 표에 저장한다.
 * 사람 ID(world_ids)·근거 문장(rationale)·작업명·인원은 넘기지 않는다(5.4).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class DailyReportService implements DisposableBean {

    /** 현장이 한국이라 하루를 Asia/Seoul 로 자른다(현장별 시간대 칸이 없다) */
    static final ZoneId SITE_ZONE = ZoneId.of("Asia/Seoul");
    /** 엔진에 넘기는 이벤트 상한. 집계(event_summary)는 전부를 센다 */
    static final int MAX_EVENTS = 100;
    private static final List<String> SEVERITIES = List.of("CRITICAL", "HIGH", "CAUTION", "INFO");
    private static final Logger log = LoggerFactory.getLogger(DailyReportService.class);

    private final DailyReportRepository daily;
    private final TbmReportRepository reports;
    private final RiskEngineReportClient engine;
    private final SiteLookup sites;
    private final TransactionTemplate transaction;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();

    DailyReportService(DailyReportRepository daily, TbmReportRepository reports, RiskEngineReportClient engine,
            SiteLookup sites, TransactionTemplate transaction, ObjectMapper mapper, Clock clock) {
        this.daily = daily;
        this.reports = reports;
        this.engine = engine;
        this.sites = sites;
        this.transaction = transaction;
        this.mapper = mapper;
        this.clock = clock;
    }

    public ObjectNode start(DailyReportRequest request) {
        LocalDate date = parseDate(request == null ? null : request.reportDate());
        String siteId = sites.siteId();
        DailyStart started = transaction.execute(s -> daily.createGenerating(siteId, date, clock.instant()));
        try {
            executor.submit(() -> runGeneration(siteId, started));
        } catch (RejectedExecutionException e) {
            // 서버가 내려가는 중이다. GENERATING 으로 두면 그 날짜가 재시작 전까지 409 로 막힌다
            fail(started.reportId(), "INTERRUPTED", null, null, null, null);
        }
        ObjectNode accepted = mapper.createObjectNode();
        accepted.put("report_id", started.reportId());
        accepted.put("report_date", date.toString());
        accepted.put("report_version", started.reportVersion());
        accepted.put("status", "GENERATING");
        return accepted;
    }

    public ObjectNode getOne(long reportId) {
        return daily.findOne(sites.siteId(), reportId)
                .orElseThrow(() -> new BusinessException(ErrorCode.Report.REPORT_NOT_FOUND));
    }

    public ObjectNode getByDate(String date) {
        return daily.findByDate(sites.siteId(), parseDate(date));
    }

    /** 비우면 오늘. 오늘보다 뒤는 아직 일어난 일이 없어 받지 않는다 */
    LocalDate parseDate(String raw) {
        LocalDate today = LocalDate.now(clock.withZone(SITE_ZONE));
        if (raw == null || raw.isBlank()) {
            return today;
        }
        LocalDate date;
        try {
            date = LocalDate.parse(raw.strip());
        } catch (DateTimeParseException e) {
            throw new BusinessException(ErrorCode.Report.INVALID_DAILY_DATE);
        }
        if (date.isAfter(today)) {
            throw new BusinessException(ErrorCode.Report.INVALID_DAILY_DATE);
        }
        return date;
    }

    private void runGeneration(String siteId, DailyStart start) {
        Long ruleVersion = null;
        try {
            Instant from = start.reportDate().atStartOfDay(SITE_ZONE).toInstant();
            Instant to = start.reportDate().plusDays(1).atStartOfDay(SITE_ZONE).toInstant();
            List<EventFact> all = daily.findEvents(from, to);
            ObjectNode summary = summary(all);
            // 구역·근거 조항도 엔진에 넘기는 이벤트 기준이다(잘린 이벤트의 구역·규칙까지 넣지 않는다)
            List<EventFact> facts = pick(all);
            ArrayNode events = events(facts);
            daily.saveFacts(start.reportId(), events, summary);

            ActiveRules rules = reports.findActiveRules(siteId);
            ruleVersion = rules.representativeRuleVersion();
            ObjectNode request = mapper.createObjectNode();
            request.put("report_kind", "DAILY");
            request.put("request_id", "rpt-" + start.reportId());
            request.put("report_date", start.reportDate().toString());
            request.put("rule_version", ruleVersion);
            request.set("events", events);
            request.set("event_summary", summary);
            request.set("zones", zones(siteId, facts));
            request.set("legal_basis", legalBasis(rules, facts));

            JsonNode res;
            try {
                res = engine.postJson("/v1/report/generate", request);
            } catch (HttpTimeoutException e) {
                fail(start.reportId(), "ENGINE_TIMEOUT", ruleVersion, null, null, null);
                return;
            } catch (EngineCallException e) {
                fail(start.reportId(), "ENGINE_UNAVAILABLE", ruleVersion, null, null, null);
                return;
            }
            String model = text(res.path("llm").path("model"));
            JsonNode subgraph = res.path("subgraph").isObject() ? res.path("subgraph") : null;
            JsonNode steps = res.path("steps").isArray() ? res.path("steps") : null;
            if ("SUCCEEDED".equals(res.path("status").asString(""))) {
                reports.completeSucceeded(start.reportId(), ruleVersion, null, null,
                        res.path("sections").isArray() ? res.path("sections") : mapper.createArrayNode(),
                        res.path("citations").isArray() ? res.path("citations") : mapper.createArrayNode(),
                        subgraph, steps, model, clock.instant());
            } else {
                String reason = text(res.path("failure_reason"));
                fail(start.reportId(), reason != null ? reason : "ENGINE_UNAVAILABLE", ruleVersion, subgraph, steps, model);
            }
        } catch (RuntimeException | Error e) {
            log.warn("Unexpected error while generating daily report {}", start.reportId(), e);
            fail(start.reportId(), "ENGINE_UNAVAILABLE", ruleVersion, null, null, null);
        }
    }

    /** 마감 기록. 이것마저 실패하면(DB 순간 끊김) 한 번 더 시도하고, 그래도 안 되면 기동 때 복구(INTERRUPTED)에 맡긴다 */
    private void fail(long reportId, String reason, Long ruleVersion, JsonNode subgraph, JsonNode steps, String model) {
        for (int attempt = 1; attempt <= 2; attempt++) {
            try {
                reports.completeFailed(reportId, reason, ruleVersion, null, null, subgraph, steps, model, clock.instant());
                return;
            } catch (RuntimeException e) {
                log.warn("Could not mark daily report {} as failed (attempt {})", reportId, attempt, e);
            }
        }
    }

    /** 상한을 넘으면 높은 등급부터 고르고(늦게 난 CRITICAL 이 빠지지 않게) 고른 것을 발생 순으로 둔다 */
    static List<EventFact> pick(List<EventFact> facts) {
        if (facts.size() <= MAX_EVENTS) {
            return facts;
        }
        return facts.stream()
                .sorted(Comparator.comparingInt(DailyReportService::severityRank).thenComparing(EventFact::raisedAt))
                .limit(MAX_EVENTS)
                .sorted(Comparator.comparing(EventFact::raisedAt))
                .toList();
    }

    private static int severityRank(EventFact f) {
        int i = SEVERITIES.indexOf(f.severity());
        return i < 0 ? SEVERITIES.size() : i;
    }

    private ArrayNode events(List<EventFact> facts) {
        ArrayNode out = mapper.createArrayNode();
        for (EventFact f : facts) {
            ObjectNode e = out.addObject();
            e.put("rule_id", f.ruleId());
            e.put("severity", f.severity());
            if (f.zoneId() == null) {
                e.putNull("zone_id");
            } else {
                e.put("zone_id", f.zoneId());
            }
            e.put("raised_at_ms", f.raisedAt().toEpochMilli());
            e.put("resolved", f.resolved());
            e.put("needs_check", f.needsCheck());
        }
        return out;
    }

    private ObjectNode summary(List<EventFact> facts) {
        ObjectNode out = mapper.createObjectNode();
        out.put("event_count", facts.size());
        out.put("resolved_count", facts.stream().filter(EventFact::resolved).count());
        out.put("needs_check_count", facts.stream().filter(EventFact::needsCheck).count());
        // events 목록이 상한으로 잘렸는지(목록과 건수가 다른 이유를 LLM·화면이 알게)
        out.put("events_truncated", facts.size() > MAX_EVENTS);
        ObjectNode severities = out.putObject("severity_counts");
        for (String s : SEVERITIES) {
            long n = facts.stream().filter(f -> s.equals(f.severity())).count();
            if (n > 0) {
                severities.put(s, n);
            }
        }
        // 규칙 번호 순(R1, R2, …, R9, R11). 같은 길이면 사전 순
        Map<String, Long> byRule = new TreeMap<>((a, b) -> a.length() != b.length() ? Integer.compare(a.length(), b.length()) : a.compareTo(b));
        facts.forEach(f -> byRule.merge(f.ruleId(), 1L, Long::sum));
        ObjectNode rules = out.putObject("rule_counts");
        byRule.forEach(rules::put);
        return out;
    }

    /** 이벤트가 난 구역의 종류·이름(후보 검색 키워드, TBM 의 zones 와 같은 모양) */
    private ArrayNode zones(String siteId, List<EventFact> facts) {
        Set<String> ids = new LinkedHashSet<>();
        facts.forEach(f -> {
            if (f.zoneId() != null && !f.zoneId().isBlank()) {
                ids.add(f.zoneId());
            }
        });
        ArrayNode out = mapper.createArrayNode();
        for (ZoneMeta z : daily.findZones(siteId, ids)) {
            out.addObject().put("zone_id", z.zoneId()).put("zone_kind", z.zoneKind()).put("label", z.label());
        }
        return out;
    }

    /** 그날 난 규칙의 고정 근거 조항. 이벤트가 없으면 활성 규칙 전체(TBM 의 공종 없음과 같은 대체) */
    private ArrayNode legalBasis(ActiveRules rules, List<EventFact> facts) {
        Set<String> fired = new LinkedHashSet<>();
        facts.forEach(f -> fired.add(f.ruleId()));
        Set<String> bases = new LinkedHashSet<>();
        for (RuleRow r : rules.rules()) {
            if (fired.isEmpty() || fired.contains(r.ruleId())) {
                TbmReportService.addSplitBasis(bases, r.legalBasis());
            }
        }
        ArrayNode out = mapper.createArrayNode();
        bases.forEach(out::add);
        return out;
    }

    private static String text(JsonNode node) {
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
