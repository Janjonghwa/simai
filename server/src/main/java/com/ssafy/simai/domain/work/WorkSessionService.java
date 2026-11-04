package com.ssafy.simai.domain.work;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.report.TbmReportService;
import com.ssafy.simai.domain.report.WorkWrapUpRepository;
import java.time.Clock;
import java.time.temporal.ChronoUnit;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WorkSessionService {
    private final WorkUnitRepository works;
    private final WorkWrapUpRepository wrapUps;
    private final TbmReportService reports;
    private final SiteLookup sites;
    private final TransactionTemplate transaction;
    private final ObjectMapper mapper;
    private final Clock clock;

    public WorkSessionService(WorkUnitRepository works, WorkWrapUpRepository wrapUps, TbmReportService reports,
            SiteLookup sites, TransactionTemplate transaction, ObjectMapper mapper, Clock clock) {
        this.works = works;
        this.wrapUps = wrapUps;
        this.reports = reports;
        this.sites = sites;
        this.transaction = transaction;
        this.mapper = mapper;
        this.clock = clock;
    }

    public ObjectNode start(long workId) {
        String site = sites.siteId();
        return transaction.execute(s -> works.startSession(site, workId, clock.instant().truncatedTo(ChronoUnit.MILLIS)));
    }

    public ObjectNode end(long workId) {
        String site = sites.siteId();
        ObjectNode response = transaction.execute(s -> {
            var now = clock.instant().truncatedTo(ChronoUnit.MILLIS);
            ObjectNode work = works.endSession(site, workId, now);
            ObjectNode out = mapper.createObjectNode();
            out.set("work_unit", work);
            out.set("wrap_up", wrapUps.prepare(workId, work, now));
            return out;
        });
        if ("GENERATING".equals(response.path("wrap_up").path("status").asString())) {
            long reportId = response.path("wrap_up").path("report_id").asLong();
            reports.startWrapUp(reportId, wrapUps.request(reportId, (ObjectNode) response.path("work_unit")));
        }
        return response;
    }
}
