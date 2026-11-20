package com.ssafy.simai.domain.watch;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import java.time.Clock;
import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionalEventListener;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 워치 배정 알림(watch_binding_notice, I4 EC2 → 워치, S15P31A101-510, 설계 docs/design/server-watch-alert.md 배정 알림 절).
 * <ul>
 *   <li>배정이 생기거나 풀리면(WatchBindingChanged, 커밋 뒤) 연결된 그 워치에 지금 상태를 한 번 보낸다
 *   <li>워치가 (재)연결되면(WatchConnected) 지금 상태를 한 번 보낸다(초기 동기화). 이때는 이전에 보냈던 것과 같아도 다시 보낸다: 워치 쪽 상태는 사라졌을 수 있다
 *   <li>같은 상태(BOUND 의 같은 World ID, UNBOUND)를 이어서 반복해 보내지 않는다. 방식(MOTION_MATCH → MANUAL)만 바뀐 것은 새 상태가 아니다
 *   <li>끊긴 워치에는 보내지 않고 보낸 기록도 지운다(다시 연결될 때의 동기화가 보낸다)
 * </ul>
 * 알림·DB 읽기는 한 스레드에서 차례로 한다: 배정을 만드는 쪽(대조 스레드·REST)과 WebSocket 스레드를 DB 지연으로 막지 않고, 연결·변경 이벤트의 순서를 지킨다.
 * 보낸 상태 기억은 그 스레드에서만 만진다. 사람 이름은 싣지 않는다(익명 World ID 뿐, 11.4-5).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WatchBindingNotifier implements AutoCloseable {

    static final String NOTICE_VERSION = "1.0.0";

    private static final Logger log = LoggerFactory.getLogger(WatchBindingNotifier.class);

    private final WatchHub hub;
    private final WatchBindings bindings;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final ExecutorService executor = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "watch-binding-notice");
        t.setDaemon(true);
        return t;
    });
    /** 워치마다 마지막으로 보낸 상태 키("BOUND|world" 또는 "UNBOUND"). executor 스레드에서만 만진다 */
    private final Map<String, String> sent = new HashMap<>();

    WatchBindingNotifier(WatchHub hub, WatchBindings bindings, ObjectMapper mapper, Clock clock) {
        this.hub = hub;
        this.bindings = bindings;
        this.mapper = mapper;
        this.clock = clock;
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onChanged(WatchBindingChanged changed) {
        submit(() -> notice(changed.deviceId(), changed.unbindReason(), false));
    }

    @EventListener
    void onConnected(WatchConnected connected) {
        submit(() -> notice(connected.deviceId(), null, true));
    }

    /** 시험용: 앞서 낸 이벤트를 다 처리할 때까지 기다린다 */
    void awaitIdle() throws Exception {
        executor.submit(() -> { }).get();
    }

    private void submit(Runnable task) {
        try {
            executor.execute(() -> {
                try {
                    task.run();
                } catch (RuntimeException e) {
                    log.warn("워치 배정 알림을 보내지 못함", e);
                }
            });
        } catch (RejectedExecutionException e) {
            // 서버가 내려가는 중이다
        }
    }

    private void notice(String deviceId, String unbindReason, boolean force) {
        if (!hub.isConnected(deviceId)) {
            sent.remove(deviceId);
            return;
        }
        Optional<WatchBindings.Binding> binding = bindings.activeForDevice(deviceId);
        String key = binding.map(b -> "BOUND|" + b.worldId()).orElse("UNBOUND");
        if (!force && key.equals(sent.get(deviceId))) {
            return;
        }
        ObjectNode payload = mapper.createObjectNode();
        long now = clock.millis();
        if (binding.isPresent()) {
            payload.put("state", "BOUND");
            // 계약 watch_binding_notice 1.0.0 method 는 MOTION_MATCH·WAVE·MANUAL 닫힌 enum 이다.
            // DB 의 DEMO_SINGLE 은 자동 배정이라 계약을 깨지 않게 MOTION_MATCH 로 싣는다(S15P31A101-826)
            payload.put("method", binding.get().demoSingle() ? "MOTION_MATCH" : binding.get().method());
            payload.put("world_id", binding.get().worldId());
        } else {
            payload.put("state", "UNBOUND");
            payload.putNull("method");
            payload.putNull("world_id");
            if (unbindReason != null) {
                payload.put("unbind_reason", unbindReason);
            }
        }
        payload.put("sent_at", now);
        ObjectNode message = I1Envelope.outbound(mapper, now, "watch_binding_notice", NOTICE_VERSION, payload);
        if (hub.send(deviceId, message)) {
            sent.put(deviceId, key);
        } else {
            sent.remove(deviceId);
        }
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }
}
