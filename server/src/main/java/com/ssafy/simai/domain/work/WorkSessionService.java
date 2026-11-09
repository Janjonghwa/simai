package com.ssafy.simai.domain.work;

import com.ssafy.simai.domain.config.ConfigBundleService;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.report.TbmReportService;
import com.ssafy.simai.domain.report.WorkWrapUpRepository;
import com.ssafy.simai.domain.layout.LayoutZoneService;
import java.time.Clock;
import java.time.temporal.ChronoUnit;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Service;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 작업 시작·종료(명세 8.3). 상태 바꾸기와 안전모 요구(ppe_policy)를 실은 새 구성 묶음 만들기를 한 트랜잭션으로 한다
 * (명세 8.10·12.4, S15P31A101-518). 묶음을 만들지 못하면(보정 없음·규칙 없음·공개 주소 미설정) 시작·종료도 하지 않는다.
 * 종료 묶음이 빠지면 작업이 없는데 Tier 0 가 안전모를 계속 요구하고, 시작 묶음이 빠지면 작업 중 요구가 없다.
 * Tier 0 로 보내기는 커밋 뒤에 하고 config_ack 는 기다리지 않는다(응답 전까지 '적용 대기', 명세 3.2).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WorkSessionService {
    private final WorkUnitRepository works;
    private final WorkWrapUpRepository wrapUps;
    private final TbmReportService reports;
    private final SiteLookup sites;
    private final ConfigBundleService bundles;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final LayoutZoneService layoutZones;

    public WorkSessionService(WorkUnitRepository works, WorkWrapUpRepository wrapUps, TbmReportService reports,
            SiteLookup sites, ConfigBundleService bundles, ObjectMapper mapper, Clock clock, LayoutZoneService layoutZones) {
        this.works = works;
        this.wrapUps = wrapUps;
        this.reports = reports;
        this.sites = sites;
        this.bundles = bundles;
        this.mapper = mapper;
        this.clock = clock;
        this.layoutZones = layoutZones;
    }

    public ObjectNode start(long workId, long userId) {
        String site = sites.siteId();
        var now = clock.instant().truncatedTo(ChronoUnit.MILLIS);
        return bundles.publishWith(() -> {
            layoutZones.lock();
            ObjectNode work = works.startSession(site, workId, now);
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
            ObjectNode work = works.endSession(site, workId, now);
            layoutZones.end(site, workId, java.time.OffsetDateTime.ofInstant(now, java.time.ZoneOffset.UTC));
            ObjectNode out = mapper.createObjectNode();
            out.set("work_unit", work);
            out.set("wrap_up", wrapUps.prepare(workId, work, now));
            return out;
        }, true, userId).changes();
        if ("GENERATING".equals(response.path("wrap_up").path("status").asString())) {
            long reportId = response.path("wrap_up").path("report_id").asLong();
            reports.startWrapUp(reportId, wrapUps.request(reportId, (ObjectNode) response.path("work_unit")));
        }
        return response;
    }
}
