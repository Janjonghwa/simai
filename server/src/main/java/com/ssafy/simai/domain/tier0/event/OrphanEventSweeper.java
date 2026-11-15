package com.ssafy.simai.domain.tier0.event;

import com.ssafy.simai.domain.gpu.GpuSessionEnded;
import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
import com.ssafy.simai.domain.work.WorkEnded;
import com.ssafy.simai.global.security.Tier0TokenAuthenticationFilter;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionalEventListener;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Tier 0 가 사라져 해소 전이가 다시 오지 않을 LIVE 이벤트를 EC2 가 마감한다(S15P31A101-722, ServerEventCloser).
 * Tier 0 가 살아 있으면(그 gateway_id 의 I1 연결이 열려 있으면) 아무것도 하지 않는다: 작업 종료는 Tier 0 가 종료 묶음으로 스스로 닫고(-679),
 * Tier 0 가 들고 있지 않은 이벤트는 그 Tier 0 의 스냅샷을 받을 때 닫는다(EventStateSnapshotIngestor, NOT_IN_SNAPSHOT).
 * <ul>
 *   <li>GPU 세션 종료(SESSION_ENDED): 그 gateway_id 로 등록한 GPU 세션이 모두 끝났고 연결도 없으면 닫는다. 세션이 끝날 때(커밋 뒤)와
 *       주기(simai.event-close.sweep-interval, 관리 명령 등 다른 프로세스가 끝낸 세션)에 본다. 새 세션이 열려 있으면(밀림) 닫지 않는다:
 *       그 Tier 0 가 붙어 스냅샷을 보내면 NOT_IN_SNAPSHOT 으로, 붙지 못하고 끝나면 SESSION_ENDED 로 닫힌다.
 *   <li>작업 종료(WORK_ENDED, 커밋 뒤): 연결도 열린 GPU 세션도 없는 gateway_id 의 열린 이벤트를 닫는다(환경 변수 토큰으로 붙던 Tier 0 등).
 *       열린 GPU 세션이 있으면 그 Tier 0 가 다시 붙어 종료 묶음으로 닫거나 세션이 끝날 때 닫힌다.
 * </ul>
 * 끝난 GPU 세션의 토큰으로 연 연결은 곧 끊기므로(GpuSessionSweeper) 살아 있는 연결로 치지 않는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class OrphanEventSweeper {

    private static final Logger log = LoggerFactory.getLogger(OrphanEventSweeper.class);

    /** 해소 전 LIVE 이벤트가 있는 gateway_id */
    private static final String OPEN_GATEWAYS = """
            SELECT DISTINCT gateway_id FROM risk_event
            WHERE source = 'LIVE' AND lifecycle <> 'RESOLVED' AND gateway_id IS NOT NULL
            """;
    private static final String SESSION_OPEN =
            "NOT EXISTS (SELECT 1 FROM gpu_session_event x WHERE x.session_id = s.session_id AND x.kind = 'ENDED')";

    /** 열린 이벤트가 있고, GPU 세션이 하나 이상 있으며 모두 끝난 gateway_id 와 그 마지막 세션·끝난 까닭 */
    private static final String ENDED_SESSION_GATEWAYS = """
            SELECT g.gateway_id, last.session_id::text AS session_id, last.end_reason
            FROM (%s) g
            CROSS JOIN LATERAL (
                SELECT s.session_id, x.end_reason FROM gpu_session s
                LEFT JOIN gpu_session_event x ON x.session_id = s.session_id AND x.kind = 'ENDED'
                WHERE s.gateway_id = g.gateway_id
                ORDER BY s.registered_at DESC, s.session_id
                LIMIT 1
            ) last
            WHERE NOT EXISTS (SELECT 1 FROM gpu_session s WHERE s.gateway_id = g.gateway_id AND %s)
            """.formatted(OPEN_GATEWAYS, SESSION_OPEN);

    /** 열린 이벤트가 있고 열린 GPU 세션이 없는 gateway_id */
    private static final String NO_OPEN_SESSION_GATEWAYS = """
            SELECT g.gateway_id FROM (%s) g
            WHERE NOT EXISTS (SELECT 1 FROM gpu_session s WHERE s.gateway_id = g.gateway_id AND %s)
            """.formatted(OPEN_GATEWAYS, SESSION_OPEN);

    private static final String ENDED_SESSIONS =
            "SELECT session_id::text FROM gpu_session_event WHERE kind = 'ENDED' AND session_id IN (:ids)";

    private final ServerEventCloser closer;
    private final I1ConnectionRegistry connections;
    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final ApplicationEventPublisher publisher;

    OrphanEventSweeper(ServerEventCloser closer, I1ConnectionRegistry connections, NamedParameterJdbcTemplate jdbc,
            TransactionTemplate transaction, ApplicationEventPublisher publisher) {
        this.closer = closer;
        this.connections = connections;
        this.jdbc = jdbc;
        this.transaction = transaction;
        this.publisher = publisher;
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onSessionEnded(GpuSessionEnded event) {
        sweep();
    }

    /** 실패해도 세션 종료·주기 작업을 막지 않는다. 다음 주기에 다시 본다 */
    @Scheduled(initialDelayString = "${simai.event-close.sweep-interval:60s}",
            fixedDelayString = "${simai.event-close.sweep-interval:60s}")
    void sweep() {
        try {
            closeEndedSessions();
        } catch (RuntimeException e) {
            log.warn("끝난 GPU 세션의 열린 이벤트 마감 실패: error={}", e.getClass().getSimpleName());
        }
    }

    /** 작업 종료는 이미 커밋됐다. 실패해도 응답을 바꾸지 않고, GPU 세션 Tier 0 의 이벤트는 주기 작업이 다시 본다 */
    @TransactionalEventListener(fallbackExecution = true)
    void onWorkEnded(WorkEnded event) {
        try {
            closeAfterWorkEnd(event.workId());
        } catch (RuntimeException e) {
            log.warn("작업 종료 뒤 열린 이벤트 마감 실패: work={} error={}", event.workId(), e.getClass().getSimpleName());
        }
    }

    /** 연결도 열린 GPU 세션도 없는 Tier 0 의 열린 이벤트를 닫는다. @return 닫은 이벤트 수 */
    public int closeAfterWorkEnd(long workId) {
        Set<String> live = liveGateways();
        int closed = 0;
        for (String gatewayId : jdbc.queryForList(NO_OPEN_SESSION_GATEWAYS, Map.of(), String.class)) {
            if (live.contains(gatewayId)) {
                continue; // 살아 있는 Tier 0 는 종료 묶음으로 스스로 닫는다(-679)
            }
            closed += closeGateway(gatewayId, ServerEventCloser.Kind.WORK_ENDED, String.valueOf(workId),
                    "작업 종료 때 이 이벤트를 들고 있던 Tier 0 가 연결돼 있지 않고 열린 GPU 세션도 없다. 관측 해소 아님");
        }
        return closed;
    }

    /** GPU 세션이 모두 끝났고 살아 있는 연결도 없는 Tier 0 의 열린 이벤트를 닫는다. @return 닫은 이벤트 수 */
    public int closeEndedSessions() {
        Set<String> live = liveGateways();
        int closed = 0;
        for (Map<String, Object> row : jdbc.queryForList(ENDED_SESSION_GATEWAYS, Map.of())) {
            String gatewayId = (String) row.get("gateway_id");
            if (live.contains(gatewayId)) {
                continue;
            }
            String reason = "GPU 세션이 끝나(" + row.get("end_reason") + ") 이 이벤트를 들고 있던 Tier 0 가 사라졌다. 관측 해소 아님";
            closed += closeGateway(gatewayId, ServerEventCloser.Kind.SESSION_ENDED, (String) row.get("session_id"), reason);
        }
        return closed;
    }

    private int closeGateway(String gatewayId, ServerEventCloser.Kind kind, String triggerRef, String reason) {
        List<UUID> closed = transaction.execute(status -> {
            List<UUID> ids = closer.close(gatewayId, kind, triggerRef, reason, Set.of(), null);
            if (!ids.isEmpty()) {
                // 커밋 뒤 /topic/events 로 해소된 모습을 보낸다(진행 중 카드를 내린다)
                publisher.publishEvent(new RiskEventsDelivered(ids));
            }
            return ids;
        });
        int count = closed == null ? 0 : closed.size();
        if (count > 0) {
            log.warn("Tier 0 가 없어 열린 LIVE 이벤트를 EC2 가 마감했다: gateway={} kind={} trigger={} count={}",
                    gatewayId, kind, triggerRef, count);
        }
        return count;
    }

    /** 열린 I1 연결의 gateway_id. 끝난 GPU 세션 토큰으로 연 연결(곧 끊김)은 빼고 본다 */
    private Set<String> liveGateways() {
        Map<UUID, Set<String>> bySession = new HashMap<>();
        Set<String> live = new HashSet<>();
        for (I1Connection connection : connections.all()) {
            String gatewayId = connection.gatewayId();
            if (gatewayId == null || !connection.isOpen()) {
                continue;
            }
            UUID session = sessionOf(connection);
            if (session == null) {
                live.add(gatewayId);
            } else {
                bySession.computeIfAbsent(session, k -> new HashSet<>()).add(gatewayId);
            }
        }
        if (!bySession.isEmpty()) {
            Set<String> ended = new HashSet<>(jdbc.queryForList(ENDED_SESSIONS,
                    new MapSqlParameterSource("ids", bySession.keySet()), String.class));
            bySession.forEach((session, gateways) -> {
                if (!ended.contains(session.toString())) {
                    live.addAll(gateways);
                }
            });
        }
        return live;
    }

    private static UUID sessionOf(I1Connection connection) {
        String name = connection.principalName();
        String prefix = Tier0TokenAuthenticationFilter.SESSION_PRINCIPAL_PREFIX;
        if (name == null || !name.startsWith(prefix)) {
            return null;
        }
        try {
            return UUID.fromString(name.substring(prefix.length()));
        } catch (IllegalArgumentException e) {
            return null;
        }
    }
}
