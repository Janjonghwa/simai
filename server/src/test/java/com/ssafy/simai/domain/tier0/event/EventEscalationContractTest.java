package com.ssafy.simai.domain.tier0.event;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.context.ApplicationEventPublisher;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * event_escalate_result 1.0.0 및 risk_event_transition 2.1.0 escalation 계약 위반 단위 시험(S15P31A101-276).
 * DB 없이 계약 예시(정상·invalid)로 메시지 검증기를 확인한다.
 */
class EventEscalationContractTest {

    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @Test
    void event_escalate_result_계약_위반_메시지는_InvalidI1Message_로_거부한다() throws Exception {
        ApplicationEventPublisher noOpPublisher = event -> { };
        EventEscalateResultIngestor ingestor = new EventEscalateResultIngestor(null, noOpPublisher);
        assertThat(ingestor.msgType()).isEqualTo("event_escalate_result");
        assertThat(ingestor.supportedMajors()).containsExactly(1);

        // 1. contracts/examples/invalid/event_escalate_result.rejected_without_reason.json
        I1Envelope rejectedWithoutReason = envelope(EXAMPLES.resolve("invalid/event_escalate_result.rejected_without_reason.json"));
        assertThatThrownBy(() -> ingestor.ingest(rejectedWithoutReason, Instant.now(), null))
                .isInstanceOf(InvalidI1Message.class);

        // 2. ACCEPTED 인데 event_sequence 가 없음
        ObjectNode acceptedNoSeq = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("event_escalate_result.json")));
        ((ObjectNode) acceptedNoSeq.get("payload")).remove("event_sequence");
        assertThatThrownBy(() -> ingestor.ingest(toEnvelope(acceptedNoSeq), Instant.now(), null))
                .isInstanceOf(InvalidI1Message.class);

        // 3. ACCEPTED 인데 reject_reason 이 있음
        ObjectNode acceptedWithReason = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("event_escalate_result.json")));
        ((ObjectNode) acceptedWithReason.get("payload")).put("reject_reason", "NOT_UPWARD");
        assertThatThrownBy(() -> ingestor.ingest(toEnvelope(acceptedWithReason), Instant.now(), null))
                .isInstanceOf(InvalidI1Message.class);

        // 4. REJECTED 인데 event_sequence 가 있음
        ObjectNode rejectedWithSeq = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("event_escalate_result.not_upward.json")));
        ((ObjectNode) rejectedWithSeq.get("payload")).put("event_sequence", 3);
        assertThatThrownBy(() -> ingestor.ingest(toEnvelope(rejectedWithSeq), Instant.now(), null))
                .isInstanceOf(InvalidI1Message.class);

        // 5. 알 수 없는 필드 포함
        ObjectNode unknownField = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("event_escalate_result.json")));
        ((ObjectNode) unknownField.get("payload")).put("extra_field", "x");
        assertThatThrownBy(() -> ingestor.ingest(toEnvelope(unknownField), Instant.now(), null))
                .isInstanceOf(InvalidI1Message.class);
    }

    @Test
    void risk_event_transition_의_escalation_계약_위반_예시는_DB_접근_전에_InvalidI1Message_로_거부한다() throws Exception {
        RiskEventRepository repo = new RiskEventRepository(null);

        for (String invalidFile : new String[] {
                "invalid/risk_event_transition.escalation_on_raised.json",
                "invalid/risk_event_transition.escalation_on_resumed.json",
                "invalid/risk_event_transition.v2_0_escalation.json"
        }) {
            I1Envelope env = envelope(EXAMPLES.resolve(invalidFile));
            assertThatThrownBy(() -> repo.recordTransition(env, "REALTIME", Instant.now(), 0))
                    .as(invalidFile)
                    .isInstanceOf(InvalidI1Message.class);
        }
    }

    private static I1Envelope envelope(Path path) throws Exception {
        return toEnvelope((ObjectNode) MAPPER.readTree(Files.readString(path)));
    }

    private static I1Envelope toEnvelope(ObjectNode node) {
        JsonNode payload = node.get("payload");
        return new I1Envelope(
                node.get("schema_version").asString(),
                1,
                node.get("msg_type").asString(),
                UUID.fromString(node.get("msg_id").asString()),
                Instant.ofEpochMilli(node.get("sent_at").longValue()),
                node.get("source_id").asString(),
                payload);
    }
}
