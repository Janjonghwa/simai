package com.ssafy.simai.domain.health;

import com.ssafy.simai.domain.realtime.RealtimePublisher;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import java.util.Objects;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * /topic/health(viewer_health)를 1초마다 보낸다(명세 12.4). 받은 값(Tier 0 health·하트비트·Scene 버전)이 바뀌어 화면이 다르게 보일
 * 상태(System Health·원격 단절·연결·보정·구성·Scene·워치 연결)가 바뀌면 다음 주기를 기다리지 않고 보낸다. 끊김과 DB 쪽 변경(새 COMMITTED Scene,
 * 구성 적용)은 1초 주기가 잡는다.
 * 만든 상태마다 장애 기록(OutageRecorder)도 맞춘다.
 * <p>모든 발행은 이 전용 스레드 하나에서 한다. I1 수신·하트비트 요청 스레드를 붙잡지 않고(DB·브로커가 느려도), 공유 스케줄러의 다른 일
 * (journal_ack 보내기 등)이 막혀도 1초 주기가 멈추지 않으며, 메시지가 만든 순서대로 나간다(앞 상태가 뒤에 나가지 않는다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class HealthPublisher implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(HealthPublisher.class);
    private static final long PERIOD_MS = 1_000;

    private final ViewerHealthService health;
    private final RealtimePublisher publisher;
    private final OutageRecorder outages;
    private final ScheduledExecutorService executor = Executors.newSingleThreadScheduledExecutor(
            Thread.ofPlatform().name("health-publisher").factory());
    /** 바뀜 알림이 이미 걸려 있으면 더 걸지 않는다(world_state 10 Hz 등) */
    private final AtomicBoolean changePending = new AtomicBoolean();
    private String lastShape;

    HealthPublisher(ViewerHealthService health, RealtimePublisher publisher, HealthState state, OutageRecorder outages) {
        this.health = health;
        this.publisher = publisher;
        this.outages = outages;
        state.onChange(this::changed);
        executor.scheduleAtFixedRate(() -> run(true), PERIOD_MS, PERIOD_MS, TimeUnit.MILLISECONDS);
    }

    private void changed() {
        if (changePending.compareAndSet(false, true)) {
            try {
                executor.execute(() -> {
                    changePending.set(false);
                    run(false);
                });
            } catch (RejectedExecutionException e) {
                changePending.set(false); // 서버가 내려가는 중이다
            }
        }
    }

    /** 이 스레드에서만 부른다 */
    private void run(boolean always) {
        try {
            ObjectNode message = health.message();
            String shape = shape(message.get("payload"));
            if (always || !Objects.equals(lastShape, shape)) {
                lastShape = shape;
                publisher.publish(RealtimeTopics.HEALTH, message.get("msg_type").asString(),
                        message.get("schema_version").asString(), message.get("payload"));
            }
            // 화면 알림을 먼저 보낸다. 기록(DB)이 느리거나 실패해도 알림은 늦지 않는다(G3)
            outages.observe(message.get("payload"));
        } catch (RuntimeException e) {
            // 한 번 실패해도 다음 주기는 돈다
            log.warn("viewer_health 를 만들거나 장애를 기록하지 못함", e);
        }
    }

    /** 화면이 다르게 보일 값만(시각·데이터 연령·원문 제외) */
    private static String shape(JsonNode p) {
        return String.join("|", p.path("system_health").asString(""), p.path("remote_state").asString(""),
                p.path("tier0_link").asString(""), p.path("laptop").path("link").asString(""),
                p.path("calibration_state").asString(""), p.path("config").toString(), p.path("scene").toString(),
                watchLinks(p.path("watches")), p.path("held_events_revision").asString(""));
    }

    /** 워치마다 연결 상태만(배터리·마지막 수신 시각은 1초 주기로 충분하다) */
    private static String watchLinks(JsonNode watches) {
        StringBuilder s = new StringBuilder();
        for (JsonNode item : watches.path("items")) {
            s.append(item.path("device_id").asString("")).append('=').append(item.path("link").asString("")).append(',');
        }
        return s.toString();
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }
}
