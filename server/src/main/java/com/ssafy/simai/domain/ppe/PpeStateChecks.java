package com.ssafy.simai.domain.ppe;

import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.util.HashSet;
import java.util.Set;
import tools.jackson.databind.JsonNode;

/**
 * ppe_state 계약 설명에 "계약 시험"으로 적었지만 JSON Schema 로는 걸 수 없는 규칙(contracts/messages/ppe_state.schema.json).
 * 스키마 검사를 통과한 payload 만 받는다고 보고, 어기면 InvalidI1Message 를 던진다.
 * <ul>
 *   <li>한 메시지에 같은 world_id 는 하나</li>
 *   <li>item_state: expires_at_ms ≥ last_evidence_at_ms, 둘 다 person_state.capture_time_ms 이하인 근거에서 나온다</li>
 *   <li>근거(evidence)의 촬영 시각도 capture_time_ms 이하</li>
 *   <li>DB(timestamptz·integer)가 받지 못하는 시각·정수는 DB 오류가 아니라 계약 위반으로 버린다</li>
 * </ul>
 */
final class PpeStateChecks {

    /** PostgreSQL timestamptz 가 받는 범위 안(9999-12-31) */
    static final long MAX_EPOCH_MS = 253_402_300_799_999L;

    private PpeStateChecks() {
    }

    static void check(JsonNode payload) {
        time(payload.get("stream_epoch"), "stream_epoch");
        if (!payload.get("state_sequence").canConvertToLong()) {
            throw new InvalidI1Message("state_sequence 가 정수 범위를 넘는다");
        }
        Set<String> worldIds = new HashSet<>();
        for (JsonNode person : payload.get("people")) {
            String worldId = person.get("world_id").asString();
            if (!worldIds.add(worldId)) {
                throw new InvalidI1Message("한 메시지에 같은 world_id 가 두 번 있다");
            }
            if (!person.get("identity_revision").canConvertToInt()) {
                throw new InvalidI1Message("identity_revision 이 정수 범위를 넘는다");
            }
            long capture = time(person.get("capture_time_ms"), "capture_time_ms");
            item(person.get("helmet"), "helmet", capture);
            item(person.get("vest"), "vest", capture);
        }
    }

    private static void item(JsonNode item, String name, long capture) {
        if (!item.get("valid_cells").canConvertToInt()) {
            throw new InvalidI1Message(name + ".valid_cells 가 정수 범위를 넘는다");
        }
        Long lastEvidence = optionalTime(item.get("last_evidence_at_ms"), name + ".last_evidence_at_ms");
        Long expires = optionalTime(item.get("expires_at_ms"), name + ".expires_at_ms");
        if (lastEvidence != null && lastEvidence > capture) {
            throw new InvalidI1Message(name + ".last_evidence_at_ms 가 capture_time_ms 보다 늦다");
        }
        if (lastEvidence != null && expires != null && expires < lastEvidence) {
            throw new InvalidI1Message(name + ".expires_at_ms 가 last_evidence_at_ms 보다 이르다");
        }
        for (JsonNode evidence : item.get("evidence")) {
            if (time(evidence.get("capture_time_ms"), name + ".evidence.capture_time_ms") > capture) {
                throw new InvalidI1Message(name + ".evidence 의 촬영 시각이 capture_time_ms 보다 늦다");
            }
        }
    }

    private static Long optionalTime(JsonNode node, String field) {
        return node == null || node.isNull() ? null : time(node, field);
    }

    private static long time(JsonNode node, String field) {
        if (!node.canConvertToLong() || node.asLong() > MAX_EPOCH_MS) {
            throw new InvalidI1Message(field + " 이 받을 수 있는 시각을 넘는다");
        }
        return node.asLong();
    }
}
