package com.ssafy.simai.domain.gpu;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
import com.ssafy.simai.global.security.Tier0TokenAuthenticationFilter;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionalEventListener;

/**
 * GPU 세션 무응답 정리와 끝난 세션의 I1 연결 끊기(S15P31A101-699, 웹 서버에서만).
 * <ul>
 * <li>주기(simai.gpu-session.sweep-interval): 마지막 보고 뒤 stale-after 가 지났고 그 세션 Tier 0 토큰의 I1 연결도 없는 세션을 끝낸다.</li>
 * <li>세션이 끝나면(커밋 뒤 이벤트) 그 세션 토큰으로 인증한 I1 연결을 바로 끊는다. Tier 0 는 다시 붙으려다 401 을 받는다.
 * 관리 명령(다른 프로세스)이 끝낸 세션은 이벤트가 오지 않으므로 주기 작업이 끊는다.</li>
 * </ul>
 * 연결과 세션은 업그레이드 요청 principal 이름(gpu-session:&lt;세션 ID&gt;)으로 잇는다. 환경 변수 토큰으로 붙은 연결은 건드리지 않는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class GpuSessionSweeper {

    private static final Logger log = LoggerFactory.getLogger(GpuSessionSweeper.class);

    private final I1ConnectionRegistry connections;
    private final GpuSessionService sessions;

    GpuSessionSweeper(I1ConnectionRegistry connections, GpuSessionService sessions) {
        this.connections = connections;
        this.sessions = sessions;
    }

    @Scheduled(initialDelayString = "${simai.gpu-session.sweep-interval:30s}",
            fixedDelayString = "${simai.gpu-session.sweep-interval:30s}")
    public void sweep() {
        Set<UUID> connected = connectedSessions();
        List<UUID> stale = sessions.endStale(connected);
        if (!stale.isEmpty()) {
            log.warn("GPU 세션 {} 를 무응답으로 끝냈다(마지막 보고 뒤 {} 동안 보고·I1 연결 없음). 토큰을 폐기했다", stale, sessions.staleAfter());
        }
        Set<UUID> ended = sessions.endedAmong(connected);
        if (!ended.isEmpty()) {
            close(ended);
        }
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onEnded(GpuSessionEnded event) {
        close(Set.of(event.sessionId()));
    }

    /** 그 세션 Tier 0 토큰으로 연 I1 연결이 지금 열려 있는지(관리 화면 tier0_link) */
    boolean tier0Connected(UUID sessionId) {
        return connectedSessions().contains(sessionId);
    }

    private Set<UUID> connectedSessions() {
        Set<UUID> ids = new HashSet<>();
        for (I1Connection connection : connections.all()) {
            if (connection.isOpen()) {
                sessionOf(connection).ifPresent(ids::add);
            }
        }
        return ids;
    }

    private void close(Set<UUID> ended) {
        for (I1Connection connection : connections.all()) {
            Optional<UUID> session = sessionOf(connection);
            if (session.isPresent() && ended.contains(session.get()) && connection.isOpen()) {
                log.info("끝난 GPU 세션 {} 의 I1 연결 {} 을 끊는다", session.get(), connection.id());
                connection.close("GPU 세션 종료");
            }
        }
    }

    private static Optional<UUID> sessionOf(I1Connection connection) {
        String name = connection.principalName();
        if (name == null || !name.startsWith(Tier0TokenAuthenticationFilter.SESSION_PRINCIPAL_PREFIX)) {
            return Optional.empty();
        }
        try {
            return Optional.of(UUID.fromString(name.substring(Tier0TokenAuthenticationFilter.SESSION_PRINCIPAL_PREFIX.length())));
        } catch (IllegalArgumentException e) {
            return Optional.empty();
        }
    }
}
