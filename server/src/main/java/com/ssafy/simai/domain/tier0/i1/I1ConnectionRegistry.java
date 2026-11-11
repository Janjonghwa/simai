package com.ssafy.simai.domain.tier0.i1;

import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.OptionalLong;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;

/**
 * 열려 있는 I1 연결. 주기 작업(journal_ack 몰아 보내기 등)이 돈다.
 * Tier 0(gateway_id)마다 I1 메시지를 마지막으로 받은 때(받는 쪽 단조 시계 ns)도 둔다. 연결이 끊겨도 지우지 않아
 * "Tier 0 가 마지막으로 살아 있던 때"를 끊긴 뒤에도 잴 수 있다(S15P31A101-595, Tier0HeldEvents). 메모리에만 둔다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class I1ConnectionRegistry {

    private final Map<String, I1Connection> connections = new ConcurrentHashMap<>();
    private final Map<String, Long> lastReceivedNanos = new ConcurrentHashMap<>();

    void add(I1Connection connection) {
        connections.put(connection.id(), connection);
    }

    void remove(String id) {
        connections.remove(id);
    }

    public Collection<I1Connection> all() {
        return List.copyOf(connections.values());
    }

    /** 이 Tier 0 에서 I1 메시지(종류 무관)를 받았다. nanos 는 System.nanoTime() 값이다 */
    public void received(String gatewayId, long nanos) {
        lastReceivedNanos.merge(gatewayId, nanos, (old, now) -> now - old > 0 ? now : old);
    }

    /** 이 Tier 0 에서 마지막으로 I1 메시지를 받은 때(System.nanoTime() 값). 이 EC2 가 뜬 뒤 받은 적 없으면 비어 있다 */
    public OptionalLong lastReceivedNanos(String gatewayId) {
        Long nanos = lastReceivedNanos.get(gatewayId);
        return nanos == null ? OptionalLong.empty() : OptionalLong.of(nanos);
    }
}
