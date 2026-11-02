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
 * event_close_result 1.x 계약 위반 단위 시험(S15P31A101-221). DB 없이 계약 예시(정상·invalid)로 메시지 검증기를 확인한다.
 */
class EventCloseContractTest {

    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @Test
    void event_close_result_계약_위반_메시지는_InvalidI1Message_로_거부한다() throws Exception {
        ApplicationEventPublisher noOpPublisher = event -> { };
        EventCloseResultIngestor ingestor = new EventCloseResultIngestor(null, noOpPublisher, new EventCloseWaiters());
        assertThat(ingestor.msgType()).isEqualTo("event_close_result");
        assertThat(ingestor.supportedMajors()).containsExactly(1);

        for (String invalidFile : new String[] {
                "invalid/event_close_result.accepted_without_sequence.json",
                "invalid/event_close_result.rejected_without_reason.json"
        }) {
            I1Envelope env = envelope(example(invalidFile));
            assertThatThrownBy(() -> ingestor.ingest(env, Instant.now(), null))
                    .as(invalidFile)
                    .isInstanceOf(InvalidI1Message.class);
        }

        // ACCEPTED 인데 reject_reason 이 있음
        ObjectNode acceptedWithReason = example("event_close_result.json");
        ((ObjectNode) acceptedWithReason.get("payload")).put("reject_reason", "NOT_ACTIVE");
        assertThatThrownBy(() -> ingestor.ingest(envelope(acceptedWithReason), Instant.now(), null))
                .isInstanceOf(InvalidI1Message.class);

        // REJECTED 인데 event_sequence 가 있음
        ObjectNode rejectedWithSeq = example("event_close_result.rejected.json");
        ((ObjectNode) rejectedWithSeq.get("payload")).put("event_sequence", 3);
        assertThatThrownBy(() -> ingestor.ingest(envelope(rejectedWithSeq), Instant.now(), null))
                .isInstanceOf(InvalidI1Message.class);

        // 계약에 없는 거부 사유·결과
        ObjectNode unknownReason = example("event_close_result.rejected.json");
        ((ObjectNode) unknownReason.get("payload")).put("reject_reason", "NOT_UPWARD");
        assertThatThrownBy(() -> ingestor.ingest(envelope(unknownReason), Instant.now(), null))
                .isInstanceOf(InvalidI1Message.class);
        ObjectNode unknownResult = example("event_close_result.json");
        ((ObjectNode) unknownResult.get("payload")).put("result", "CLOSED");
        assertThatThrownBy(() -> ingestor.ingest(envelope(unknownResult), Instant.now(), null))
                .isInstanceOf(InvalidI1Message.class);

        // 알 수 없는 필드
        ObjectNode unknownField = example("event_close_result.json");
        ((ObjectNode) unknownField.get("payload")).put("extra_field", "x");
        assertThatThrownBy(() -> ingestor.ingest(envelope(unknownField), Instant.now(), null))
                .isInstanceOf(InvalidI1Message.class);
    }

    private static ObjectNode example(String name) throws Exception {
        return (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve(name)));
    }

    private static I1Envelope envelope(ObjectNode node) {
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
