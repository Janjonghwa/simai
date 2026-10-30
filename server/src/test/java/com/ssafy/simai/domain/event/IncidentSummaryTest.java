package com.ssafy.simai.domain.event;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.domain.job.JobStateChanged;
import com.ssafy.simai.domain.tier0.event.RiskEventsDelivered;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Array;
import java.sql.ResultSet;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
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

class IncidentSummaryTest {

    private final JsonMapper mapper = JsonMapper.builder().build();
    private final NamedParameterJdbcTemplate jdbc = mock(NamedParameterJdbcTemplate.class);
    private final ObjectStorage storage = mock(ObjectStorage.class);
    private final AnalysisJobService jobs = mock(AnalysisJobService.class);
    private final ApplicationEventPublisher publisher = mock(ApplicationEventPublisher.class);
    private final ContractSchemas schemas = new ContractSchemas();
    private final Instant now = Instant.parse("2026-10-30T00:00:00Z");
    private final IncidentSummaryService service = new IncidentSummaryService(
            jdbc, mapper, storage, jobs, publisher, Clock.fixed(now, ZoneOffset.UTC));

    private ObjectNode exampleInput;
    private ObjectNode exampleResult;
    private UUID eventId;

    @BeforeEach
    void setUp() {
        exampleInput = (ObjectNode) example("job_request.incident_summary.json").at("/payload/input");
        exampleResult = (ObjectNode) example("job_callback.incident_summary.json").at("/payload/result");
        eventId = UUID.fromString(exampleInput.get("event_id").asString());
        when(storage.uri(anyString())).thenAnswer(call -> "s3://simai-bucket/" + call.getArgument(0));
    }

    @Test
    void LIVE_이벤트와_클립이_있으면_계약에_맞는_incident_summary_작업_1개를_만들고_발행한다() throws Exception {
        mockEventRow("LIVE", 2);
        when(jdbc.query(contains("FROM incident_summary"), any(SqlParameterSource.class), anyRowMapper()))
                .thenReturn(List.of());
        mockClipRow();
        when(jdbc.query(contains("SELECT transition FROM risk_event_update"), any(SqlParameterSource.class), anyRowMapper()))
                .thenReturn(List.of("RAISED", "UPDATED"));
        when(jdbc.query(contains("FROM world_track_state"), any(SqlParameterSource.class), anyRowMapper()))
                .thenReturn(List.of());
        when(jdbc.query(contains("FROM zone"), any(SqlParameterSource.class), anyRowMapper()))
                .thenReturn(List.of(mapper.createObjectNode().put("zone_id", "zone-scaffold-02").put("judge_type", "VOLUME_3D")));
        when(jobs.enqueue(any())).thenReturn("job-sum-1");
        when(jdbc.queryForObject(contains("INSERT INTO incident_summary"), any(SqlParameterSource.class), eq(Long.class)))
                .thenReturn(11L);

        IncidentSummaryService.SummaryResponse res = service.requestSummary(eventId, 7L);

        assertThat(res.created()).isTrue();
        assertThat(res.body().get("summary_id").asLong()).isEqualTo(11L);
        assertThat(res.body().get("job_id").asString()).isEqualTo("job-sum-1");
        assertThat(res.body().get("status").asString()).isEqualTo("QUEUED");
        assertThat(res.body().get("window_start_ms").asLong()).isEqualTo(1760342380000L);
        assertThat(res.body().get("window_end_ms").asLong()).isEqualTo(1760342420000L);

        var specCaptor = ArgumentCaptor.forClass(JobSpec.class);
        verify(jobs).enqueue(specCaptor.capture());
        JobSpec spec = specCaptor.getValue();
        assertThat(spec.jobType()).isEqualTo("incident_summary");
        assertThat(spec.timeoutMs()).isEqualTo(120_000);
        assertThat(spec.maxAttempts()).isEqualTo(2);
        assertThat(spec.eventId()).isEqualTo(eventId);
        assertThat(spec.requestedBy()).isEqualTo(7L);

        ObjectNode requestEnvelope = example("job_request.incident_summary.json");
        ((ObjectNode) requestEnvelope.get("payload")).set("input", spec.input());
        assertThat(schemas.violations("messages/job_request", requestEnvelope.toString())).isEmpty();

        var delivered = ArgumentCaptor.forClass(RiskEventsDelivered.class);
        verify(publisher).publishEvent(delivered.capture());
        assertThat(delivered.getValue().eventIds()).containsExactly(eventId);
    }

    @Test
    void 진행중인_요약이_있으면_새_작업을_만들지_않고_기존_항목을_돌려준다() throws Exception {
        mockEventRow("LIVE", 2);
        ObjectNode active = mapper.createObjectNode()
                .put("summary_id", 5L)
                .put("job_id", "job-active")
                .put("status", "RUNNING")
                .put("requested_at", now.toEpochMilli());
        when(jdbc.query(contains("FROM incident_summary"), any(SqlParameterSource.class), anyRowMapper()))
                .thenReturn(List.of(active));

        IncidentSummaryService.SummaryResponse res = service.requestSummary(eventId, 7L);

        assertThat(res.created()).isFalse();
        assertThat(res.body().get("summary_id").asLong()).isEqualTo(5L);
        assertThat(res.body().get("job_id").asString()).isEqualTo("job-active");
        assertThat(res.body().get("status").asString()).isEqualTo("RUNNING");
        verifyNoInteractions(jobs, publisher);
    }

    @Test
    void 없는_이벤트는_404_비LIVE는_409_클립없음은_409를_던진다() throws Exception {
        when(jdbc.query(contains("FROM risk_event"), any(SqlParameterSource.class), anyRowMapper()))
                .thenReturn(List.of());
        assertThatThrownBy(() -> service.requestSummary(eventId, 1L))
                .isInstanceOf(BusinessException.class)
                .extracting(e -> ((BusinessException) e).getErrorCode())
                .isEqualTo(ErrorCode.Event.EVENT_NOT_FOUND);

        mockEventRow("REPLAY", 2);
        assertThatThrownBy(() -> service.requestSummary(eventId, 1L))
                .isInstanceOf(BusinessException.class)
                .extracting(e -> ((BusinessException) e).getErrorCode())
                .isEqualTo(ErrorCode.Event.EVENT_NOT_LIVE);

        mockEventRow("LIVE", 2);
        when(jdbc.query(contains("FROM incident_summary"), any(SqlParameterSource.class), anyRowMapper()))
                .thenReturn(List.of());
        when(jdbc.query(contains("FROM media_clip"), any(SqlParameterSource.class), anyRowMapper()))
                .thenReturn(List.of());
        assertThatThrownBy(() -> service.requestSummary(eventId, 1L))
                .isInstanceOf(BusinessException.class)
                .extracting(e -> ((BusinessException) e).getErrorCode())
                .isEqualTo(ErrorCode.Event.CLIP_NOT_READY);
        verifyNoInteractions(jobs, publisher);
    }

    @Test
    void 성공_콜백은_결과를_저장하고_이벤트_등급은_건드리지_않으며_발행한다() throws Exception {
        mockAnalysisJob();
        when(jdbc.queryForList(contains("SELECT source FROM risk_event"), any(SqlParameterSource.class), eq(String.class)))
                .thenReturn(List.of("LIVE"));
        when(jdbc.update(contains("SET status = 'SUCCEEDED'"), any(SqlParameterSource.class))).thenReturn(1);

        service.finished(new JobFinished("job-sum-1", "incident_summary", "SUCCEEDED", exampleResult, null));

        var sqlCaptor = ArgumentCaptor.forClass(String.class);
        verify(jdbc).update(sqlCaptor.capture(), any(SqlParameterSource.class));
        assertThat(sqlCaptor.getValue()).contains("UPDATE incident_summary", "SET status = 'SUCCEEDED'")
                .doesNotContain("UPDATE risk_event");
        verify(publisher).publishEvent(new RiskEventsDelivered(List.of(eventId)));
    }

    @Test
    void 잘못된_콜백_결과와_등급_필드_포함은_거부하고_저장하지_않는다() throws Exception {
        mockAnalysisJob();
        when(jdbc.queryForList(contains("SELECT source FROM risk_event"), any(SqlParameterSource.class), eq(String.class)))
                .thenReturn(List.of("LIVE"));

        for (String badCase : List.of("event_id", "event_sequence", "window_start_ms", "evidence_out_of_range",
                "severity", "proposed_severity")) {
            ObjectNode bad = exampleResult.deepCopy();
            switch (badCase) {
                case "event_id" -> bad.put("event_id", "0199df4d-0000-7000-8000-000000000999");
                case "event_sequence" -> bad.put("event_sequence", 99);
                case "window_start_ms" -> bad.put("window_start_ms", 1760342300000L);
                case "evidence_out_of_range" -> bad.putArray("evidence_timestamps_ms").add(1760342999999L);
                case "severity" -> bad.put("severity", "CRITICAL");
                case "proposed_severity" -> bad.put("proposed_severity", "CRITICAL");
            }
            assertThatThrownBy(() -> service.finished(new JobFinished("job-sum-1", "incident_summary", "SUCCEEDED", bad, null)))
                    .isInstanceOf(BusinessException.class)
                    .extracting(e -> ((BusinessException) e).getErrorCode())
                    .isEqualTo(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        verify(jdbc, never()).update(anyString(), any(SqlParameterSource.class));
        verifyNoInteractions(publisher);
    }

    @Test
    void 대여_실패_타임아웃_상태_변화를_반영하고_발행한다() throws Exception {
        when(jdbc.queryForList(contains("SET status = 'RUNNING'"), any(SqlParameterSource.class), eq(UUID.class)))
                .thenReturn(List.of(eventId));
        service.stateChanged(new JobStateChanged("job-sum-1", "incident_summary", "LEASED"));
        verify(publisher).publishEvent(new RiskEventsDelivered(List.of(eventId)));

        mockAnalysisJob();
        when(jdbc.update(contains("SET status = :status"), any(SqlParameterSource.class))).thenReturn(1);
        ObjectNode err = mapper.createObjectNode().put("code", "VLM_TIMEOUT").put("message", "시간 초과");
        service.finished(new JobFinished("job-sum-1", "incident_summary", "TIMED_OUT", null, err));

        var paramsCaptor = ArgumentCaptor.forClass(SqlParameterSource.class);
        verify(jdbc).update(contains("SET status = :status"), paramsCaptor.capture());
        assertThat(paramsCaptor.getValue().getValue("status")).isEqualTo("TIMED_OUT");
        assertThat(paramsCaptor.getValue().getValue("errorCode")).isEqualTo("VLM_TIMEOUT");
    }

    private void mockEventRow(String source, int sequence) throws Exception {
        ResultSet rs = mock(ResultSet.class);
        when(rs.getString("source")).thenReturn(source);
        when(rs.getString("gateway_id")).thenReturn("tier0-01");
        when(rs.getString("rule_id")).thenReturn("FALL_FROM_HEIGHT");
        when(rs.getString("severity")).thenReturn("CRITICAL");
        when(rs.getString("zone_id")).thenReturn("zone-scaffold-02");
        when(rs.getString("world_ids")).thenReturn("[\"w-12\"]");
        when(rs.getString("metrics")).thenReturn("{\"height_m\":2.4,\"dwell_s\":1.2}");
        when(rs.getObject("event_sequence")).thenReturn(sequence);
        when(rs.getTimestamp("observation_at")).thenReturn(Timestamp.from(Instant.ofEpochMilli(1760342400100L)));
        when(rs.getTimestamp("raised_at")).thenReturn(Timestamp.from(Instant.ofEpochMilli(1760342400100L)));
        when(rs.getTimestamp("updated_at")).thenReturn(Timestamp.from(Instant.ofEpochMilli(1760342400100L)));
        when(jdbc.query(contains("FROM risk_event"), any(SqlParameterSource.class), anyRowMapper()))
                .thenAnswer(call -> {
                    RowMapper<Object> rm = call.getArgument(2);
                    return List.of(rm.mapRow(rs, 0));
                });
    }

    private void mockClipRow() throws Exception {
        ResultSet rs = mock(ResultSet.class);
        String key = "clips/tier0-01/" + eventId + "/0/cam-01/clip-0199df4d-0001-7000-8000-000000000001.mp4";
        when(rs.getString("clip_id")).thenReturn("clip-0199df4d-0001-7000-8000-000000000001");
        when(rs.getString("camera_id")).thenReturn("cam-01");
        when(rs.getString("object_key")).thenReturn(key);
        when(rs.getTimestamp("clip_start_capture_at")).thenReturn(Timestamp.from(Instant.ofEpochMilli(1760342380000L)));
        when(rs.getTimestamp("clip_end_capture_at")).thenReturn(Timestamp.from(Instant.ofEpochMilli(1760342420000L)));
        when(rs.getTimestamp("anchor_capture_at")).thenReturn(Timestamp.from(Instant.ofEpochMilli(1760342400100L)));
        when(rs.getInt("frame_count")).thenReturn(400);
        when(rs.getString("container")).thenReturn("MP4_H264");
        when(rs.getInt("event_sequence")).thenReturn(2);
        Array emptyArr = mock(Array.class);
        when(emptyArr.getArray()).thenReturn(new Object[0]);
        when(rs.getArray(anyString())).thenReturn(emptyArr);
        when(jdbc.query(contains("FROM media_clip"), any(SqlParameterSource.class), anyRowMapper()))
                .thenAnswer(call -> {
                    RowMapper<Object> rm = call.getArgument(2);
                    return List.of(rm.mapRow(rs, 0));
                });
    }

    private void mockAnalysisJob() throws Exception {
        ResultSet rs = mock(ResultSet.class);
        when(rs.getString("input")).thenReturn(exampleInput.toString());
        when(rs.getObject("finished_at", OffsetDateTime.class)).thenReturn(now.atOffset(ZoneOffset.UTC));
        when(jdbc.query(contains("FROM analysis_job"), any(SqlParameterSource.class), anyRowMapper()))
                .thenAnswer(call -> {
                    RowMapper<Object> rm = call.getArgument(2);
                    return List.of(rm.mapRow(rs, 0));
                });
    }

    private static <T> RowMapper<T> anyRowMapper() {
        return org.mockito.ArgumentMatchers.any();
    }

    private ObjectNode example(String file) {
        try {
            return (ObjectNode) mapper.readTree(Files.readString(
                    Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples", file)));
        } catch (java.io.IOException e) {
            throw new java.io.UncheckedIOException(e);
        }
    }
}
