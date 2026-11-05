package com.ssafy.simai.domain.ppe;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** 스키마가 못 거는 ppe_state 계약 시험(PpeStateChecks)과 만료 판단(isStale). DB 없이 돈다 */
class PpeStateChecksTest {

    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @Test
    void 계약_예시는_통과한다() throws Exception {
        assertThatCode(() -> PpeStateChecks.check(payload("ppe_state.json"))).doesNotThrowAnyException();
        assertThatCode(() -> PpeStateChecks.check(payload("ppe_state.no_people.json"))).doesNotThrowAnyException();
    }

    @Test
    void 같은_world_id_가_두_번이면_버린다() throws Exception {
        ObjectNode p = payload("ppe_state.json");
        ((ObjectNode) p.get("people").get(1)).put("world_id", p.get("people").get(0).get("world_id").asString());
        assertThatThrownBy(() -> PpeStateChecks.check(p)).isInstanceOf(InvalidI1Message.class).hasMessageContaining("world_id");
    }

    @Test
    void 만료가_마지막_근거보다_이르거나_근거가_촬영_시각보다_늦으면_버린다() throws Exception {
        ObjectNode expires = payload("ppe_state.json");
        ObjectNode helmet = (ObjectNode) expires.get("people").get(0).get("helmet");
        helmet.put("expires_at_ms", helmet.get("last_evidence_at_ms").asLong() - 1);
        assertThatThrownBy(() -> PpeStateChecks.check(expires)).hasMessageContaining("expires_at_ms");

        ObjectNode late = payload("ppe_state.json");
        long capture = late.get("people").get(0).get("capture_time_ms").asLong();
        ((ObjectNode) late.get("people").get(0).get("vest").get("evidence").get(0)).put("capture_time_ms", capture + 1);
        assertThatThrownBy(() -> PpeStateChecks.check(late)).hasMessageContaining("evidence");

        ObjectNode lastAfter = payload("ppe_state.json");
        ((ObjectNode) lastAfter.get("people").get(0).get("helmet")).put("last_evidence_at_ms", capture + 1);
        assertThatThrownBy(() -> PpeStateChecks.check(lastAfter)).hasMessageContaining("last_evidence_at_ms");
    }

    @Test
    void DB_가_받지_못하는_시각은_버린다() throws Exception {
        ObjectNode p = payload("ppe_state.json");
        ((ObjectNode) p.get("people").get(0)).put("capture_time_ms", PpeStateChecks.MAX_EPOCH_MS + 1);
        assertThatThrownBy(() -> PpeStateChecks.check(p)).hasMessageContaining("capture_time_ms");
    }

    @Test
    void 촬영_시각이_수신보다_10초_넘게_이르면_만료로_본다() throws Exception {
        ObjectNode person = (ObjectNode) payload("ppe_state.json").get("people").get(0);
        Instant capture = Instant.ofEpochMilli(person.get("capture_time_ms").asLong());
        assertThat(PpeStateIngestor.isStale(person, capture.plusSeconds(10))).isFalse();
        assertThat(PpeStateIngestor.isStale(person, capture.plusSeconds(10).plusMillis(1))).isTrue();
    }

    private static ObjectNode payload(String file) throws Exception {
        return (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve(file))).get("payload");
    }
}
