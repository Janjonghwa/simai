package com.ssafy.simai.domain.tier0.i1;

import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;

/** 열려 있는 I1 연결. 주기 작업(journal_ack 몰아 보내기 등)이 돈다. */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class I1ConnectionRegistry {

    private final Map<String, I1Connection> connections = new ConcurrentHashMap<>();

    void add(I1Connection connection) {
        connections.put(connection.id(), connection);
    }

    void remove(String id) {
        connections.remove(id);
    }

    public Collection<I1Connection> all() {
        return List.copyOf(connections.values());
    }
}
