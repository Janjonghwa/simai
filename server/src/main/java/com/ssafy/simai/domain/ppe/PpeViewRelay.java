package com.ssafy.simai.domain.ppe;

import com.ssafy.simai.domain.realtime.RealtimePublisher;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionalEventListener;

/**
 * 사람별 현재 PPE 상태가 바뀌면(커밋 뒤) 그 Tier 0 의 스냅샷을 /topic/ppe(ppe_view 1.0.0)로 보낸다(S15P31A101-531, 명세 12.4).
 * 한 스레드에서 보내고, 밀리면 Tier 0 마다 최신 스냅샷만 보낸다(사이 것은 replaced 로 센다). 스냅샷은 보낼 때 DB 에서 다시 만들므로
 * 늘 그 순간의 현재 상태다. 실패해도 I1 수신·저장을 멈추지 않는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class PpeViewRelay implements AutoCloseable {

    static final String MSG_TYPE = "ppe_view";
    static final String SCHEMA_VERSION = "1.0.0";
    private static final Logger log = LoggerFactory.getLogger(PpeViewRelay.class);

    private final PpeViewRepository views;
    private final RealtimePublisher publisher;
    private final ExecutorService executor = Executors.newSingleThreadExecutor(Thread.ofPlatform().name("stomp-ppe").factory());
    /** 보내기를 걸어 두고 아직 시작하지 않은 Tier 0 */
    private final Map<String, Boolean> pending = new ConcurrentHashMap<>();

    PpeViewRelay(PpeViewRepository views, RealtimePublisher publisher) {
        this.views = views;
        this.publisher = publisher;
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onStored(PpeStateStored stored) {
        String sourceId = stored.sourceId();
        if (pending.putIfAbsent(sourceId, Boolean.TRUE) != null) {
            // 같은 Tier 0 보내기가 이미 걸려 있다. 그때 DB 에서 다시 만드므로 이번 변경도 실린다
            publisher.count(RealtimeTopics.PPE, "replaced");
            return;
        }
        try {
            executor.execute(() -> {
                pending.remove(sourceId);
                try {
                    publisher.publish(RealtimeTopics.PPE, MSG_TYPE, SCHEMA_VERSION, views.snapshot(sourceId));
                } catch (RuntimeException e) {
                    publisher.count(RealtimeTopics.PPE, "failed");
                    log.warn("PPE 스냅샷 만들기 실패: source_id={}", sourceId, e);
                }
            });
        } catch (RejectedExecutionException e) {
            pending.remove(sourceId);
            publisher.count(RealtimeTopics.PPE, "failed");
            log.warn("PPE 발행 대기열에 넣지 못함: source_id={}", sourceId);
        }
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }
}
