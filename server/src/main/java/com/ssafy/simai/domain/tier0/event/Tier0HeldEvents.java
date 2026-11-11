package com.ssafy.simai.domain.tier0.event;

import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
import java.time.Instant;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.Map;
import java.util.OptionalLong;
import java.util.Set;
import java.util.UUID;
import java.util.function.LongSupplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 지금 Tier 0 가 활성으로 들고 있는 LIVE 이벤트(S15P31A101-595, 명세 3.2 재동기화). 관제 화면의 「진행 중」을 가르는 데만 쓰는 파생값이다.
 * risk_event 를 바꾸지 않는다(EC2 는 전이를 만들지 않는다, AGENTS 규칙 4·명세 3.2 "복원한 활성 이벤트는 자동 해소하지 않는다").
 *
 * <ul>
 *   <li>스냅샷(event_state_snapshot)을 분류할 때 그 Tier 0 의 집합을 active_events 로 통째로 바꾼다.
 *   <li>스냅샷 뒤(동기화가 끝난 뒤) 현재 상태를 바꾼 실시간 전이로 고친다: RESOLVED 는 빼고, RAISED·UPDATED 는 넣는다.
 *       ACKNOWLEDGED 는 생명주기를 바꾸지 않아 그대로 둔다. 동기화 중 재전송 전이는 보지 않는다(이어지는 스냅샷이 바꾼다).
 *   <li>들고 있음 = 집합에 있고, 그 Tier 0 에서 I1 메시지를 마지막으로 받은 지 유예 시간(graceMs, 기본 60초) 안이다.
 *       짧은 I1 끊김에는 집합을 그대로 둬 경보가 꺼지지 않고(같은 Tier 0 가 다시 붙어 스냅샷을 보내면 그것으로 맞춘다),
 *       GPU 를 끄면 유예 시간 뒤 1초 주기 작업이 집합을 비운다.
 *   <li>집합이 한꺼번에 바뀌면(스냅샷으로 달라짐·유예 만료) 판(revision)을 올린다. 관제 앱은 viewer_health.held_events_revision 이
 *       바뀌면 목록을 다시 받는다. 전이 하나로 바뀐 것은 그 전이의 /topic/events 가 알리므로 판을 올리지 않는다.
 *   <li>메모리에만 둔다. EC2 가 다시 뜨면 비어 있다가 Tier 0 가 다시 붙어 스냅샷을 보내면 찬다(그 사이는 모두 「지난 관제」).
 * </ul>
 * 끊김은 받는 쪽 단조 시계(System.nanoTime)로 잰다(viewer_health tier0_link 와 같은 방식).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class Tier0HeldEvents {

    /**
     * 마지막 I1 수신 뒤 이벤트를 계속 「진행 중」으로 두는 시간(ms). Tier 0 I1 클라이언트가 반쯤 끊긴 연결을 알아채는 데 최대 20초
     * (ping 10초 + pong 기다림 10초, ai/services/tier0/i1_client.py), 첫 재접속 대기 1초·접속 시간 제한 10초, 두 번째 재접속 2초·10초를
     * 더하면 43초다. 여기에 재전송·스냅샷 여유를 둬 60초로 한다. 근거는 설계 문서 S15P31A101-595.
     */
    static final long DEFAULT_GRACE_MS = 60_000;

    private static final Logger log = LoggerFactory.getLogger(Tier0HeldEvents.class);

    private final I1ConnectionRegistry connections;
    private final long graceNanos;
    private final LongSupplier nanoTime;
    /** 판 앞머리. EC2 가 다시 뜨면 달라져 관제 앱이 전과 같은 번호로 헷갈리지 않는다 */
    private final String boot;
    /** gateway_id → 들고 있는 event_id. 이 객체로 잠근다 */
    private final Map<String, Set<UUID>> held = new HashMap<>();
    private long revision;

    @Autowired
    public Tier0HeldEvents(I1ConnectionRegistry connections,
            @Value("${simai.events.held-grace-ms:" + DEFAULT_GRACE_MS + "}") long graceMs) {
        this(connections, graceMs, System::nanoTime, Long.toString(Instant.now().toEpochMilli()));
    }

    Tier0HeldEvents(I1ConnectionRegistry connections, long graceMs, LongSupplier nanoTime, String boot) {
        if (graceMs <= 0) {
            throw new IllegalArgumentException("simai.events.held-grace-ms 는 0 보다 커야 한다");
        }
        this.connections = connections;
        this.graceNanos = graceMs * 1_000_000L;
        this.nanoTime = nanoTime;
        this.boot = boot;
    }

    /** 스냅샷의 활성 이벤트로 이 Tier 0 의 집합을 바꾼다. 달라졌으면 판을 올린다 */
    public synchronized void replace(String gatewayId, Set<UUID> active) {
        Set<UUID> previous = held.getOrDefault(gatewayId, Set.of());
        if (previous.equals(active)) {
            return;
        }
        if (active.isEmpty()) {
            held.remove(gatewayId);
        } else {
            held.put(gatewayId, new HashSet<>(active));
        }
        revision++;
        log.info("Tier 0 가 들고 있는 이벤트를 스냅샷으로 바꿈: gateway={} before={} after={}", gatewayId, previous.size(), active.size());
    }

    /** 현재 상태를 바꾼 전이(transition: RAISED·UPDATED·RESOLVED·ACKNOWLEDGED). 동기화 중 전이도 받는다(-621) */
    public synchronized void apply(String gatewayId, UUID eventId, String transition) {
        if ("RESOLVED".equals(transition)) {
            Set<UUID> set = held.get(gatewayId);
            if (set != null && set.remove(eventId) && set.isEmpty()) {
                held.remove(gatewayId);
            }
        } else if ("RAISED".equals(transition) || "UPDATED".equals(transition)) {
            held.computeIfAbsent(gatewayId, g -> new HashSet<>()).add(eventId);
        }
    }

    /** 이 Tier 0 가 지금(유예 시간 안) 이 이벤트를 활성으로 들고 있는가. 해소 여부는 부르는 쪽이 따로 본다 */
    public synchronized boolean isHeld(String gatewayId, UUID eventId) {
        if (gatewayId == null) {
            return false;
        }
        Set<UUID> set = held.get(gatewayId);
        return set != null && set.contains(eventId) && alive(gatewayId, nanoTime.getAsLong());
    }

    /** 집합이 한꺼번에 바뀔 때마다 바뀌는 판(viewer_health.held_events_revision) */
    public synchronized String revision() {
        return boot + "-" + revision;
    }

    /** 유예 시간 넘게 I1 메시지가 없는 Tier 0 의 집합을 비운다(GPU 꺼짐) */
    @Scheduled(fixedDelay = 1000)
    public synchronized void expire() {
        long now = nanoTime.getAsLong();
        Iterator<Map.Entry<String, Set<UUID>>> it = held.entrySet().iterator();
        boolean changed = false;
        while (it.hasNext()) {
            Map.Entry<String, Set<UUID>> entry = it.next();
            if (!alive(entry.getKey(), now)) {
                log.warn("Tier 0 I1 수신이 유예 시간 넘게 없어 들고 있던 이벤트를 「진행 중」에서 뺌: gateway={} count={}",
                        entry.getKey(), entry.getValue().size());
                it.remove();
                changed = true;
            }
        }
        if (changed) {
            revision++;
        }
    }

    private boolean alive(String gatewayId, long now) {
        OptionalLong last = connections.lastReceivedNanos(gatewayId);
        return last.isPresent() && now - last.getAsLong() <= graceNanos;
    }
}
