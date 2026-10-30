package com.ssafy.simai.domain.realtime;

import com.ssafy.simai.domain.event.EventQueryRepository;
import com.ssafy.simai.domain.tier0.event.RiskEventsDelivered;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionalEventListener;

/**
 * Tier 0 에서 받아 저장한 것을 관제 앱 토픽으로 낸다(명세 3.7 4단계 "기록한 뒤 발행", 12.4).
 * <ul>
 *   <li>/topic/world: world_state payload 를 바꾸지 않고 봉투만 새로 쓴다. 발행이 밀리면 Tier 0(source_id)마다 최신 것만 보낸다
 *       (사이 것은 replaced 로 센다).
 *   <li>/topic/events: 알릴 이벤트의 현재 상태(risk_event, REST 목록과 같은 투영)를 받은 순서대로 하나씩 보낸다.
 *       하나가 실패해도 나머지는 보낸다. AUDIT_ONLY 이벤트는 보내지 않는다(명세 3.2, 기록으로만 남김).
 * </ul>
 * 저장이 끝난(커밋된) 뒤 토픽별 스레드에서 보낸다. 브로커·DB 가 느려도 I1 수신을 붙잡지 않고,
 * 이벤트 조회가 느려도 world 가, world 가 밀려도 이벤트가 막히지 않는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class Tier0Relay implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(Tier0Relay.class);
    private static final String WORLD_STATE = "world_state";
    private static final String RISK_EVENT = "risk_event";
    private static final String RISK_EVENT_VERSION = "2.1.0";
    // 이벤트 발행 대기 상한. 전이는 많아야 초당 수십 개라 넘치면 DB·브로커가 멈춘 것이다
    private static final int EVENT_QUEUE_LIMIT = 10_000;

    private final RealtimePublisher publisher;
    private final EventQueryRepository events;
    private final ExecutorService worldExecutor = Executors.newSingleThreadExecutor(
            Thread.ofPlatform().name("stomp-world").factory());
    private final ExecutorService eventExecutor = new ThreadPoolExecutor(1, 1, 0, TimeUnit.MILLISECONDS,
            new LinkedBlockingQueue<>(EVENT_QUEUE_LIMIT), Thread.ofPlatform().name("stomp-events").factory());
    /** 아직 보내지 못한 Tier 0 별 최신 world_state */
    private final Map<String, I1Envelope> pendingWorld = new ConcurrentHashMap<>();

    Tier0Relay(RealtimePublisher publisher, EventQueryRepository events) {
        this.publisher = publisher;
        this.events = events;
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onWorldState(WorldStateStored stored) {
        String sourceId = stored.envelope().sourceId();
        if (pendingWorld.put(sourceId, stored.envelope()) != null) {
            // 같은 Tier 0 의 아직 보내지 못한 이전 것을 새것으로 바꿨다. 보내는 일은 이미 걸려 있다
            publisher.count(RealtimeTopics.WORLD, "replaced");
            return;
        }
        submit(worldExecutor, RealtimeTopics.WORLD, () -> {
            I1Envelope latest = pendingWorld.remove(sourceId);
            if (latest != null) {
                publisher.publish(RealtimeTopics.WORLD, WORLD_STATE, latest.schemaVersion(), latest.payload());
            }
        });
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onRiskEvents(RiskEventsDelivered delivered) {
        for (UUID eventId : delivered.eventIds()) {
            submit(eventExecutor, RealtimeTopics.EVENTS, () -> events.findOne(eventId)
                    .filter(payload -> !"AUDIT_ONLY".equals(payload.path("delivery").asString(null)))
                    .ifPresent(payload -> publisher.publish(RealtimeTopics.EVENTS, RISK_EVENT, RISK_EVENT_VERSION, payload)));
        }
    }

    private void submit(ExecutorService executor, String topic, Runnable task) {
        try {
            executor.execute(() -> {
                try {
                    task.run();
                } catch (RuntimeException e) {
                    publisher.count(topic, "failed");
                    log.warn("STOMP 발행 준비 실패: topic={}", topic, e);
                }
            });
        } catch (RejectedExecutionException e) {
            // 서버가 내려가는 중이거나 대기열이 찼다
            publisher.count(topic, "failed");
            log.warn("STOMP 발행 대기열에 넣지 못함: topic={}", topic);
        }
    }

    @Override
    public void close() {
        worldExecutor.shutdownNow();
        eventExecutor.shutdownNow();
    }
}
