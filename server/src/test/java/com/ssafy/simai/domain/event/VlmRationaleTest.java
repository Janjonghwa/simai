package com.ssafy.simai.domain.event;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.domain.tier0.event.RiskEventsDelivered;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor.Outcome;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.ResultSet;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.core.namedparam.SqlParameterSource;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

class VlmRationaleTest {

    private final JsonMapper mapper = JsonMapper.builder().build();
    private final NamedParameterJdbcTemplate jdbc = mock(NamedParameterJdbcTemplate.class);
    private final AnalysisJobService jobs = mock(AnalysisJobService.class);
    private final ApplicationEventPublisher publisher = mock(ApplicationEventPublisher.class);
    private final ContractSchemas schemas = new ContractSchemas();
    private final Instant observed = Instant.parse("2026-10-30T00:00:00Z");
    private final ResultSet row = mock(ResultSet.class);
    private final VlmRationaleIngestor ingestor = new VlmRationaleIngestor(jdbc, mapper, schemas, jobs);
    private final VlmRationaleListener listener = new VlmRationaleListener(jdbc, mapper,
            Clock.fixed(observed.plusMillis(2100), ZoneOffset.UTC), publisher);
    private ObjectNode frameMessage;
    private ObjectNode input;
    private ObjectNode result;

    @BeforeEach
    void setUp() throws Exception {
        frameMessage = example("event_frame_ready.json");
        input = (ObjectNode) example("job_request.vlm_rationale.json").at("/payload/input");
        input.put("observation_time", observed.toEpochMilli());
        result = (ObjectNode) example("job_callback.vlm_rationale.json").at("/payload/result");
        result.put("proposed_severity", (String) null);
        when(row.getString(anyString())).thenAnswer(call -> switch ((String) call.getArgument(0)) {
            case "source" -> "LIVE";
            case "gateway_id" -> "tier0-01";
            case "delivery" -> "REALTIME";
            case "world_ids" -> "[]";
            case "metrics" -> "{}";
            default -> input.path(call.getArgument(0)).asString(null);
        });
        when(row.getInt(anyString())).thenAnswer(call -> input.path(call.getArgument(0)).asInt());
        when(row.getTimestamp("observation_at")).thenReturn(Timestamp.from(observed));
        doAnswer(call -> {
            RowMapper<Object> mapping = call.getArgument(2);
            return List.of(mapping.mapRow(row, 0));
        }).when(jdbc).query(anyString(), any(SqlParameterSource.class), org.mockito.ArgumentMatchers.<RowMapper<Object>>any());
    }

    @Test
    void CRITICAL_프레임은_BLURRED_계약_입력으로_작업을_하나_요청한다() {
        assertThat(ingestor.ingest(I1Envelope.parse(frameMessage), observed.plusMillis(100), null)).isEqualTo(Outcome.STORED);
        var spec = ArgumentCaptor.forClass(JobSpec.class);
        verify(jobs).enqueue(spec.capture());
        assertThat(spec.getValue().jobType()).isEqualTo("vlm_rationale");
        assertThat(spec.getValue().timeoutMs()).isEqualTo(5000);
        assertThat(spec.getValue().maxAttempts()).isEqualTo(2);
        JsonNode generated = spec.getValue().input();
        assertThat(generated.at("/frames/0/face_policy").asString()).isEqualTo("BLURRED");
        assertThat(generated.at("/frames/0/timestamp_uncertainty_ms"))
                .isEqualTo(frameMessage.at("/payload/frames/0/timestamp_uncertainty_ms"));
        ObjectNode request = example("job_request.vlm_rationale.json");
        ((ObjectNode) request.get("payload")).set("input", generated);
        assertThat(schemas.violations("messages/job_request", request.toString())).isEmpty();
        assertThat(generated.at("/structured_state/tracks").isArray()).isTrue();
        assertThat(generated.at("/structured_state/zone/judge_type").isNull()).isTrue();
    }

    @Test
    void 비CRITICAL_REPLAY_감사전용_5초경계는_작업을_만들지_않는다() throws Exception {
        when(row.getString("severity")).thenReturn("HIGH");
        assertThat(ingest()).isEqualTo(Outcome.DUPLICATE);
        when(row.getString("severity")).thenReturn("CRITICAL");
        when(row.getString("source")).thenReturn("REPLAY");
        assertThat(ingest()).isEqualTo(Outcome.DUPLICATE);
        when(row.getString("source")).thenReturn("LIVE");
        when(row.getString("delivery")).thenReturn("AUDIT_ONLY");
        assertThat(ingest()).isEqualTo(Outcome.DUPLICATE);
        when(row.getString("delivery")).thenReturn("REALTIME");
        assertThat(ingestor.ingest(I1Envelope.parse(frameMessage), observed.plusMillis(5000), null)).isEqualTo(Outcome.DUPLICATE);
        verifyNoInteractions(jobs);
    }

    @Test
    void 기존_작업이_있으면_다른_msg_id도_생성하지_않는다() {
        when(jdbc.queryForList(contains("SELECT 1 FROM analysis_job"), any(SqlParameterSource.class))).thenReturn(List.of(java.util.Map.of("1", 1)));
        frameMessage.put("msg_id", java.util.UUID.randomUUID().toString());
        assertThat(ingest()).isEqualTo(Outcome.DUPLICATE);
        verifyNoInteractions(jobs);
    }

    @Test
    void 계약_위반과_다른_출처는_저장하지_않는다() {
        ((ObjectNode) frameMessage.at("/payload/frames/0")).remove("timestamp_uncertainty_ms");
        assertThatThrownBy(this::ingest).isInstanceOf(InvalidI1Message.class);
        verifyNoInteractions(jdbc, jobs);
        frameMessage = example("event_frame_ready.json").put("source_id", "other-tier0");
        assertThatThrownBy(this::ingest).isInstanceOf(InvalidI1Message.class);
        verifyNoInteractions(jobs);
    }

    @Test
    void 콜백은_근거를_추가하고_등급없이_현재값을_갱신해_발행을_요청한다() {
        listenerJob();
        when(jdbc.update(anyString(), any(SqlParameterSource.class))).thenReturn(1);
        listener.finished(new JobFinished("job-vlm", "vlm_rationale", "SUCCEEDED", result, null));
        var sql = ArgumentCaptor.forClass(String.class);
        var params = ArgumentCaptor.forClass(SqlParameterSource.class);
        verify(jdbc, times(2)).update(sql.capture(), params.capture());
        assertThat(sql.getAllValues().getFirst()).contains("INSERT INTO vlm_assessment", "ON CONFLICT (job_id) DO NOTHING");
        assertThat(params.getAllValues().getFirst().getValue("elapsed")).isEqualTo(2100L);
        assertThat(sql.getAllValues().getLast()).isEqualTo("UPDATE risk_event SET rationale = :text, proposed_severity = NULL WHERE event_id = :id");
        var delivered = ArgumentCaptor.forClass(RiskEventsDelivered.class);
        verify(publisher).publishEvent(delivered.capture());
        assertThat(delivered.getValue().eventIds()).containsExactly(java.util.UUID.fromString(input.path("event_id").asString()));
    }

    @Test
    void 다른_이벤트_sequence_등급_상향의견_조항은_거부한다() {
        listenerJob();
        for (String field : List.of("event_id", "event_sequence", "severity", "proposed_severity", "clause_ids")) {
            ObjectNode bad = result.deepCopy();
            switch (field) {
                case "event_sequence" -> bad.put(field, 999);
                case "clause_ids" -> bad.putArray(field).add("가짜 조항");
                default -> bad.put(field, "HIGH");
            }
            assertThatThrownBy(() -> listener.finished(new JobFinished("job-vlm", "vlm_rationale", "SUCCEEDED", bad, null)))
                    .isInstanceOf(BusinessException.class);
        }
        verify(jdbc, never()).update(anyString(), any(SqlParameterSource.class));
        verifyNoInteractions(publisher);
    }

    @Test
    void 중복결과는_투영과_발행을_반복하지_않으며_현재_등급이_다르면_거부한다() {
        listenerJob();
        listener.finished(new JobFinished("job-vlm", "vlm_rationale", "SUCCEEDED", result, null));
        verify(jdbc, times(1)).update(anyString(), any(SqlParameterSource.class));
        verifyNoInteractions(publisher);
        when(jdbc.queryForList(contains("SELECT severity"), any(SqlParameterSource.class), eq(String.class))).thenReturn(List.of("HIGH"));
        assertThatThrownBy(() -> listener.finished(new JobFinished("job-vlm", "vlm_rationale", "SUCCEEDED", result, null)))
                .isInstanceOf(BusinessException.class);
    }

    private void listenerJob() {
        when(jdbc.queryForList(contains("SELECT input"), any(SqlParameterSource.class), eq(String.class))).thenReturn(List.of(input.toString()));
        when(jdbc.queryForList(contains("SELECT severity"), any(SqlParameterSource.class), eq(String.class))).thenReturn(List.of("CRITICAL"));
    }

    private Outcome ingest() {
        return ingestor.ingest(I1Envelope.parse(frameMessage), observed.plusMillis(100), null);
    }

    private ObjectNode example(String file) {
        try {
            return (ObjectNode) mapper.readTree(Files.readString(Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples", file)));
        } catch (java.io.IOException e) {
            throw new java.io.UncheckedIOException(e);
        }
    }
}
