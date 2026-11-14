package com.ssafy.simai.domain.work;

import com.ssafy.simai.domain.config.ConfigBundleService;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.report.TbmFormAiService;
import com.ssafy.simai.domain.report.TbmReportService;
import com.ssafy.simai.domain.report.WorkWrapUpRepository;
import com.ssafy.simai.domain.layout.LayoutZoneService;
import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 작업 시작·종료(명세 8.3). 상태 바꾸기와 안전모 요구(ppe_policy)를 실은 새 구성 묶음 만들기를 한 트랜잭션으로 한다
 * (명세 8.10·12.4, S15P31A101-518). 묶음을 만들지 못하면(보정 없음·규칙 없음·공개 주소 미설정) 시작·종료도 하지 않는다.
 * 종료 묶음이 빠지면 작업이 없는데 Tier 0 가 안전모를 계속 요구하고, 시작 묶음이 빠지면 작업 중 요구가 없다.
 * Tier 0 로 보내기는 커밋 뒤에 하고 config_ack 는 기다리지 않는다(응답 전까지 '적용 대기', 명세 3.2).
 * 시작·종료는 실행(work_run) 하나를 열고 닫는다(S15P31A101-696). 하루 작업 하나에 실행을 여러 번 할 수 있고(보고서 제출 전),
 * 실행마다 시작 묶음(ppe_policy.work_id = 이 작업)과 종료 묶음(work_id null)이 나간다. Tier 0 는 실행 사이의 종료 묶음으로 감시를 내렸다가
 * 다음 실행의 시작 묶음에서 규칙 기억을 비우고 다시 켠다(-679 의 꺼짐 → 켜짐 전환, 실행 구분 값 없이 같다).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WorkSessionService {
    private static final Logger log = LoggerFactory.getLogger(WorkSessionService.class);
    private final WorkUnitRepository works;
    private final WorkWrapUpRepository wrapUps;
    private final TbmReportService reports;
    private final SiteLookup sites;
    private final ConfigBundleService bundles;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final LayoutZoneService layoutZones;
    private final TbmFormAiService formAi;
    private final ApplicationEventPublisher events;

    public WorkSessionService(WorkUnitRepository works, WorkWrapUpRepository wrapUps, TbmReportService reports,
            SiteLookup sites, ConfigBundleService bundles, ObjectMapper mapper, Clock clock, LayoutZoneService layoutZones,
            TbmFormAiService formAi, ApplicationEventPublisher events) {
        this.formAi = formAi;
        this.events = events;
        this.works = works;
        this.wrapUps = wrapUps;
        this.reports = reports;
        this.sites = sites;
        this.bundles = bundles;
        this.mapper = mapper;
        this.clock = clock;
        this.layoutZones = layoutZones;
    }

    /** helmet·vest 는 이 작업의 안전모·조끼 요구(null 이면 저장된 값, 기본 필수, S15P31A101-654) */
    public ObjectNode start(long workId, long userId, Boolean helmet, Boolean vest) {
        String site = sites.siteId();
        var now = clock.instant().truncatedTo(ChronoUnit.MILLIS);
        return bundles.publishWith(() -> {
            layoutZones.lock();
            ObjectNode work = works.startSession(site, workId, now, helmet, vest, userId);
            // 같은 트랜잭션에서 미리보기 맵 뒤 대기 중인 세팅 굽기를 회수한다(S15P31A101-625). 묶음 발행 전이라 회수 결과와 어긋나지 않는다
            events.publishEvent(new WorkStarted(site, workId));
            Long layout = work.path("layout_id").isNumber() ? work.path("layout_id").asLong() : null;
            layoutZones.sync(site, workId, layout, userId, java.time.OffsetDateTime.ofInstant(now, java.time.ZoneOffset.UTC));
            return work;
        }, true, userId).changes();
    }

    public ObjectNode end(long workId, long userId) {
        String site = sites.siteId();
        ObjectNode response = bundles.publishWith(() -> {
            layoutZones.lock();
            var now = clock.instant().truncatedTo(ChronoUnit.MILLIS);
            ObjectNode work = works.endSession(site, workId, now, userId);
            layoutZones.end(site, workId, java.time.OffsetDateTime.ofInstant(now, java.time.ZoneOffset.UTC));
            ObjectNode out = mapper.createObjectNode();
            out.set("work_unit", work);
            out.set("wrap_up", wrapUps.prepare(workId, work, now));
            return out;
        }, true, userId).changes();
        if ("GENERATING".equals(response.path("wrap_up").path("status").asString())) {
            long reportId = response.path("wrap_up").path("report_id").asLong();
            ObjectNode work = (ObjectNode) response.path("work_unit");
            if (WorkWrapUpRepository.MODE_FORM_ENDING.equals(response.path("wrap_up").path("mode").asString())) {
                // 날짜 작업: 새 버전 없이 같은 행의 양식 종료 미팅을 채운다(S15P31A101-616 B10)
                formAi.startEnding(reportId, wrapUps.endingRequest(reportId, work));
            } else {
                reports.startWrapUp(reportId, wrapUps.request(reportId, work));
            }
        }
        return response;
    }

    /**
     * 시연 복구용: 실수로 완료한 오늘 작업을 계획(PLANNED)으로 되돌린다(S15P31A101-649, 관제 앱 화면 없음).
     * 진행 중으로 바로 되돌리지 않는다. 다시 시작은 화면의 시작(start)이 세팅 완료·서명 확인, 레이아웃 구역 다시 잇기,
     * 안전모·조끼 요구 묶음 발행을 그대로 다시 한다. 구성 묶음은 발행하지 않는다: 완료 때 이미 요구 없음 묶음이 나갔고
     * PLANNED 도 요구 없음이다. 종료 미팅(wrap-up)은 되돌리지 않고 다음 종료가 다시 채운다. 덮어쓰는 첫 실행 시각은 로그로 남긴다.
     */
    @Transactional
    public ObjectNode reopen(long workId, long userId) {
        String site = sites.siteId();
        Instant now = clock.instant().truncatedTo(ChronoUnit.MILLIS);
        ObjectNode before = works.reopenSession(site, workId, now);
        log.warn("작업 완료 되돌림(PLANNED): work_id={} user_id={} 이전 실제 시작={} 종료={}", workId, userId,
                Instant.ofEpochMilli(before.path("actual_started_at").asLong()),
                Instant.ofEpochMilli(before.path("actual_ended_at").asLong()));
        return works.findOne(site, workId).orElseThrow();
    }
}
