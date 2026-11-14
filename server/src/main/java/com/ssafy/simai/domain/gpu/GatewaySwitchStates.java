package com.ssafy.simai.domain.gpu;

import java.time.Instant;
import java.util.Comparator;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현장 게이트웨이 자동 전환 상태(S15P31A101-701, 계약 messages/gateway_switch_state)의 기기별 마지막 값. 메모리에만 둔다.
 * 운영 표시용이라 판정·알림에 쓰지 않고, EC2 재배포로 비면 게이트웨이가 5분 안에 다시 보낸다(설계 docs/design/S15P31A101-701-gateway-auto-switch.md).
 * 기록할 사건이 아니라 "지금 상태"라 표에 쌓지 않는다(이력은 서버 로그).
 */
@Component
public class GatewaySwitchStates {

    /** 받은 상태 하나 */
    public record Received(String deviceId, Instant receivedAt, JsonNode state) {
    }

    private final Map<String, Received> latest = new ConcurrentHashMap<>();
    private final ObjectMapper mapper;

    GatewaySwitchStates(ObjectMapper mapper) {
        this.mapper = mapper;
    }

    /**
     * 기기의 마지막 상태를 바꾼다.
     * @return 판단(결정·까닭·모드·주인·세션)이 앞 상태와 달라졌거나 전환을 시도했으면 true(로그에 남길 것)
     */
    public boolean record(String deviceId, JsonNode state, Instant receivedAt) {
        Received previous = latest.put(deviceId, new Received(deviceId, receivedAt, state.deepCopy()));
        if (!"NONE".equals(state.path("decision").stringValue(""))) {
            return true;
        }
        if (previous == null) {
            return true;
        }
        for (String field : new String[] {"reason_code", "mode", "owner", "auto_enabled", "session_id"}) {
            if (!Objects.equals(previous.state().get(field), state.get(field))) {
                return true;
            }
        }
        return false;
    }

    /** 가장 최근에 받은 기기 상태(관리자 GPU 세션 조회의 gateway). 받은 적 없으면 비어 있다 */
    public Optional<ObjectNode> latestView() {
        return latest.values().stream().max(Comparator.comparing(Received::receivedAt)).map(r -> {
            ObjectNode node = mapper.createObjectNode();
            node.put("device_id", r.deviceId());
            node.put("received_at_ms", r.receivedAt().toEpochMilli());
            node.set("state", r.state().deepCopy());
            return node;
        });
    }
}
