package com.ssafy.simai.domain.report;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.report.RiskEngineReportClient.EngineCallException;
import com.ssafy.simai.domain.report.TbmFormAiRepository.DraftStart;
import com.ssafy.simai.domain.report.TbmFormAiRepository.FillTarget;
import com.ssafy.simai.domain.report.TbmFormAiRepository.SetupZone;
import com.ssafy.simai.domain.work.WorkTasks;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import java.net.http.HttpTimeoutException;
import java.time.Clock;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;
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
 * 날짜 작업 TBM 보고서 양식 칸 AI 초안(B9)과 종료 미팅(B10) 연결(S15P31A101-616 2단계, docs/design/S15P31A101-616-tbm-form-draft.md).
 *
 * <p>초안: 트랜잭션 1(행 잠금 → GENERATING → 요청 고정) → 커밋 → Risk Engine {@code POST /v1/report/form-draft}
 * → 응답을 계약 {@code tbm_form_draft_result} 로 검사 → 트랜잭션 2(행 다시 잠금 → 지금 빈 칸만 채움 → form_revision+1 → SUCCEEDED).
 * 연결 실패·5xx·계약 위반은 FAILED 이고 양식은 그대로다. 새 report_version 을 만들지 않는다(작업 전 서명이 버전에 묶여 있다).
 *
 * <p>종료 미팅: 작업 종료 트랜잭션이 고정한 사실로 {@code POST /v1/report/form-ending} → 같은 행의 review.ending 이 비어 있을 때만 채운다.
 * 엔진이 실패하면 서버 결정적 문장(TbmReportService.wrapUpFallback)으로 채운다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class TbmFormAiService implements DisposableBean {

    static final String DRAFT_PATH = "/v1/report/form-draft";
    static final String ENDING_PATH = "/v1/report/form-ending";
    static final String DRAFT_REQUEST_SCHEMA = "messages/tbm_form_draft_request";
    static final String DRAFT_RESULT_SCHEMA = "messages/tbm_form_draft_result";
    static final String ENDING_REQUEST_SCHEMA = "messages/tbm_form_ending_request";
    static final String ENDING_RESULT_SCHEMA = "messages/tbm_form_ending_result";
    /** 계약 zones 상한·글자 수(tbm_form_draft_request). 넘는 구역은 싣지 않는다. */
    static final int MAX_ZONES = 50;
    static final int MAX_ZONE_NAME = 100;
    static final int MAX_ZONE_KIND = 64;

    private static final Logger log = LoggerFactory.getLogger(TbmFormAiService.class);

    private final TbmFormAiRepository drafts;
    private final TbmReportRepository reports;
    private final WorkWrapUpRepository wrapUps;
    private final SiteLookup sites;
    private final TransactionTemplate transaction;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final RiskEngineReportClient engine;
    private final ContractSchemas schemas;
    private final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();

    public TbmFormAiService(TbmFormAiRepository drafts, TbmReportRepository reports, WorkWrapUpRepository wrapUps,
            SiteLookup sites, TransactionTemplate transaction, ObjectMapper mapper, Clock clock,
            RiskEngineReportClient engine, ContractSchemas schemas) {
        this.drafts = drafts;
        this.reports = reports;
        this.wrapUps = wrapUps;
        this.sites = sites;
        this.transaction = transaction;
        this.mapper = mapper;
        this.clock = clock;
        this.engine = engine;
        this.schemas = schemas;
    }

    /**
     * 보고서 id 로 AI 양식 초안을 시작한다(POST /api/reports/tbm/{id}/form-draft). 커밋 뒤 비동기로 채운다.
     * 이미 GENERATING 이면 409 REPORT_IN_PROGRESS(report_id 포함), 양식 없음 409 REPORT_NO_FORM, 작업내용 없음 409 REPORT_FORM_NO_TASKS,
     * 예전 버전 409 REPORT_VERSION_STALE, 잠김 409 REPORT_LOCKED, 없음 404 REPORT_NOT_FOUND.
     */
    public ObjectNode startDraft(long reportId) {
        String siteId = sites.siteId();
        record Started(DraftStart start, ObjectNode request) { }
        Started started = transaction.execute(s -> {
            DraftStart start = drafts.begin(siteId, reportId);
            return new Started(start, buildDraftRequest(siteId, start));
        });
        executor.submit(() -> runDraft(siteId, started.start(), started.request()));
        ObjectNode accepted = mapper.createObjectNode();
        accepted.put("report_id", started.start().reportId());
        accepted.put("work_id", started.start().workId());
        accepted.put("report_version", started.start().reportVersion());
        accepted.put("draft_status", "GENERATING");
        accepted.put("form_revision", started.start().formRevision());
        return accepted;
    }

    /**
     * 작업 id 로 그 작업의 최신 TBM 보고서 초안을 시작한다. 세팅 완료(C17, -615)가 세팅 트랜잭션 <b>커밋 뒤</b> 부른다.
     * 오류는 {@link #startDraft} 와 같고, 작업이 없으면 404 WORK_UNIT_NOT_FOUND, 보고서가 없으면 404 REPORT_NOT_FOUND.
     */
    public ObjectNode startDraftForWork(long workId) {
        return startDraft(drafts.latestReportId(sites.siteId(), workId));
    }

    /**
     * 세팅 완료처럼 초안이 부가 작업인 호출부용. 시작하지 못하면(진행 중·작업내용 없음·잠김·보고서 없음 등) 로그만 남기고 빈 값을 돌려준다.
     */
    public Optional<ObjectNode> tryStartDraftForWork(long workId) {
        try {
            return Optional.of(startDraftForWork(workId));
        } catch (BusinessException e) {
            log.info("TBM form draft not started for work {}: {}", workId, e.getErrorCode());
            return Optional.empty();
        }
    }

    /**
     * 계약 tbm_form_draft_request. 리더·참석자·서명·계정·작업명(work_unit.name)·좌표·구역 ID 는 넣지 않는다(AGENTS 규칙 8, NFR-14).
     */
    ObjectNode buildDraftRequest(String siteId, DraftStart start) {
        ObjectNode req = mapper.createObjectNode();
        req.put("request_id", "rpt-" + start.reportId() + "-r" + start.formRevision());
        req.put("report_date", start.workDate().toString());
        ArrayNode tasks = req.putArray("tasks");
        Set<String> union = new TreeSet<>((a, b) -> Integer.compare(ruleNumber(a), ruleNumber(b)));
        for (JsonNode t : start.form().path("info").path("tasks")) {
            if (t.path("code").isString()) {
                WorkTasks.find(t.path("code").asString()).ifPresent(task -> {
                    ObjectNode row = tasks.addObject();
                    row.put("code", task.code());
                    row.put("label", task.label());
                    ArrayNode tags = row.putArray("rule_tags");
                    task.ruleTags().forEach(tags::add);
                    union.addAll(task.ruleTags());
                });
            } else if (t.path("text").isString()) {
                tasks.addObject().put("text", t.path("text").asString());
            }
        }
        ArrayNode tags = req.putArray("rule_tags");
        union.forEach(tags::add);
        ArrayNode zones = req.putArray("zones");
        Set<String> seen = new HashSet<>();
        for (SetupZone z : drafts.setupZones(siteId, start.workId())) {
            String name = z.name() == null ? "" : z.name().strip();
            String kind = z.zoneKind() == null ? "" : z.zoneKind().strip();
            if (zones.size() >= MAX_ZONES || !fits(name, MAX_ZONE_NAME) || !fits(kind, MAX_ZONE_KIND)
                    || !seen.add(name + "\u0000" + kind)) {
                continue;
            }
            // 구역에는 규칙 칸이 없다(zone 표에 rule_id 없음). 모르면 비운다(AGENTS 규칙 2).
            zones.addObject().put("name", name).put("zone_kind", kind).putNull("rule_id");
        }
        req.set("existing", TbmForm.draftFields(start.form()));
        return req;
    }

    private void runDraft(String siteId, DraftStart start, ObjectNode request) {
        long reportId = start.reportId();
        try {
            List<String> invalidRequest = schemas.violations(DRAFT_REQUEST_SCHEMA, mapper.writeValueAsString(request));
            if (!invalidRequest.isEmpty()) {
                log.warn("TBM form draft request for report {} violates contract: {}", reportId, invalidRequest);
                failDraft(reportId, "INVALID_DRAFT_REQUEST");
                return;
            }
            JsonNode response;
            try {
                response = engine.postJson(DRAFT_PATH, request);
            } catch (HttpTimeoutException e) {
                failDraft(reportId, "ENGINE_TIMEOUT");
                return;
            } catch (EngineCallException e) {
                log.warn("TBM form draft engine call failed for report {}: {}", reportId, e.getMessage());
                failDraft(reportId, "ENGINE_UNAVAILABLE");
                return;
            }
            List<String> invalid = schemas.violations(DRAFT_RESULT_SCHEMA, mapper.writeValueAsString(response));
            if (!invalid.isEmpty() || !request.path("request_id").equals(response.path("request_id"))) {
                log.warn("TBM form draft response for report {} violates contract: {}", reportId, invalid);
                failDraft(reportId, "ENGINE_CONTRACT_VIOLATION");
                return;
            }
            long ruleVersion = reports.findActiveRules(siteId).representativeRuleVersion();
            transaction.executeWithoutResult(s -> fill(start, request, response, ruleVersion));
        } catch (Exception e) {
            log.warn("Unexpected error while drafting TBM form {}", reportId, e);
            failDraft(reportId, "ENGINE_UNAVAILABLE");
        }
    }

    /** 트랜잭션 2. 그 사이 사람이 고친 칸을 포함해 지금 비어 있는 칸만 채운다. */
    private void fill(DraftStart start, ObjectNode request, JsonNode response, long ruleVersion) {
        long reportId = start.reportId();
        Optional<FillTarget> found = drafts.lockForFill(reportId);
        if (found.isEmpty() || !"GENERATING".equals(found.get().status()) || found.get().form() == null) {
            return;
        }
        FillTarget target = found.get();
        if (target.locked()) {
            failDraft(reportId, "REPORT_LOCKED");
            return;
        }
        if (!target.latest()) {
            failDraft(reportId, "REPORT_VERSION_STALE");
            return;
        }
        // 작업내용이 바뀌었으면 초안(작업명·위험)이 지금 양식과 맞지 않는다. 채우지 않고 다시 요청하게 한다.
        if (!start.form().path("info").path("tasks").equals(target.form().path("info").path("tasks"))) {
            failDraft(reportId, "FORM_TASKS_CHANGED");
            return;
        }
        ObjectNode form = target.form().deepCopy();
        if (TbmForm.fillDraft(form, response.path("form_draft"), request.path("existing"))) {
            // 저장 모양·글자 수를 한 번 더 본다(계약 상한과 같아 걸리지 않아야 한다). 어긋나면 양식은 그대로 두고 FAILED.
            ObjectNode normalized;
            try {
                normalized = TbmForm.normalize(form);
            } catch (BusinessException e) {
                log.warn("TBM form draft for report {} does not fit the form", reportId);
                failDraft(reportId, "ENGINE_CONTRACT_VIOLATION");
                return;
            }
            drafts.saveForm(reportId, normalized);
        }
        boolean fallback = response.path("fallback").asBoolean();
        String reason = response.path("fallback_reason").isString() ? response.path("fallback_reason").asString() : null;
        String model = response.path("generator").path("model").asString();
        ArrayNode steps = response.path("steps").isArray() ? (ArrayNode) response.path("steps").deepCopy() : mapper.createArrayNode();
        boolean hasFallbackStep = false;
        for (JsonNode step : steps) {
            hasFallbackStep |= "FALLBACK".equals(step.path("step").asString(""));
        }
        if (fallback && !hasFallbackStep) {
            // 대체 초안 여부·사유를 보고서 steps 에 남긴다(따로 칸을 두지 않음).
            steps.addObject().put("step", "FALLBACK").put("status", "SUCCEEDED").put("detail", "결정적 대체 초안(" + reason + ")");
        }
        reports.completeSucceeded(reportId, ruleVersion, null, null, null, response.path("citations"), null, steps, model,
                clock.instant());
        log.info("TBM form draft for report {} succeeded (fallback={}, reason={}, model={})", reportId, fallback, reason, model);
    }

    private void failDraft(long reportId, String reason) {
        reports.completeFailed(reportId, reason, null, null, null, null, null, null, clock.instant());
    }

    /** 작업 종료 트랜잭션 커밋 뒤에만 부른다. 엔진이 실패해도 결정적 문장으로 마감한다. */
    public void startEnding(long reportId, ObjectNode request) {
        executor.submit(() -> {
            String ending;
            boolean fallback = true;
            String model = "deterministic";
            try {
                List<String> invalidRequest = schemas.violations(ENDING_REQUEST_SCHEMA, mapper.writeValueAsString(request));
                if (!invalidRequest.isEmpty()) {
                    throw new IllegalArgumentException("Invalid form-ending request: " + invalidRequest);
                }
                JsonNode response = engine.postJson(ENDING_PATH, request);
                List<String> invalid = schemas.violations(ENDING_RESULT_SCHEMA, mapper.writeValueAsString(response));
                if (!invalid.isEmpty() || !request.path("request_id").equals(response.path("request_id"))) {
                    throw new IllegalArgumentException("Invalid form-ending response: " + invalid);
                }
                ending = response.path("ending").asString();
                fallback = response.path("fallback").asBoolean();
                model = response.path("model").asString();
            } catch (Exception e) {
                ending = fallbackEnding(request.path("facts"));
                log.warn("Using deterministic form ending for report {}: {}", reportId, e.getMessage());
            }
            String text = ending;
            boolean isFallback = fallback;
            String usedModel = model;
            transaction.executeWithoutResult(s -> wrapUps.completeEnding(reportId, text, isFallback, usedModel));
        });
    }

    /**
     * 재기동 때 종료 미팅을 쓰다 멈춘 날짜 작업 보고서를 고정 사실의 결정적 문장으로 마감한다.
     * 예전 작업 마무리의 INTERRUPTED 와 달리 사실이 이미 저장돼 있어 같은 결정적 문장을 만들 수 있다.
     */
    @EventListener(ApplicationReadyEvent.class)
    public int recoverEndings() {
        int count = 0;
        for (Map.Entry<Long, ObjectNode> e : wrapUps.generatingEndings()) {
            String text = fallbackEnding(e.getValue());
            transaction.executeWithoutResult(s -> wrapUps.completeEnding(e.getKey(), text, true, "deterministic"));
            count++;
        }
        if (count > 0) {
            log.info("Recovered {} interrupted TBM form ending(s) on startup", count);
        }
        return count;
    }

    /** 서버 결정적 마무리 문장(작업 마무리와 같은 사전)을 양식 상한(2000 코드 포인트)에 맞춘다. */
    static String fallbackEnding(JsonNode facts) {
        String text = TbmReportService.wrapUpFallback(facts);
        if (text.codePointCount(0, text.length()) <= TbmForm.MAX_REVIEW) {
            return text;
        }
        return text.substring(0, text.offsetByCodePoints(0, TbmForm.MAX_REVIEW));
    }

    private static boolean fits(String s, int max) {
        return !s.isEmpty() && s.codePointCount(0, s.length()) <= max && s.indexOf('\u0000') < 0;
    }

    private static int ruleNumber(String tag) {
        try {
            return Integer.parseInt(tag.substring(1));
        } catch (RuntimeException e) {
            return Integer.MAX_VALUE;
        }
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
