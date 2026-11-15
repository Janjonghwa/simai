package com.ssafy.simai.domain.job;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.ssafy.simai.domain.health.ReplayTestSwitch;
import java.sql.ResultSet;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.core.namedparam.SqlParameterSource;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

class VlmDeadlineTest {

    private final NamedParameterJdbcTemplate jdbc = mock(NamedParameterJdbcTemplate.class);
    private final ApplicationEventPublisher events = mock(ApplicationEventPublisher.class);
    private final JsonMapper mapper = JsonMapper.builder().build();
    private final JobLeaseService leases = new JobLeaseService(jdbc, mapper, new JobSignal(), events, mock(ReplayTestSwitch.class));
    private final Instant observed = Instant.parse("2026-10-30T00:00:00Z");
    private final UUID leaseId = UUID.randomUUID();
    private final ResultSet lease = mock(ResultSet.class);
    private final ResultSet job = mock(ResultSet.class);

    @BeforeEach
    void setUp() throws Exception {
        when(lease.getObject("lease_id", UUID.class)).thenReturn(leaseId);
        when(lease.getString("worker_id")).thenReturn("worker");
        when(lease.getString("job_id")).thenReturn("job-vlm");
        when(lease.getString("status")).thenReturn("ACTIVE");
        when(lease.getObject("leased_at", OffsetDateTime.class)).thenReturn(observed.plusMillis(100).atOffset(ZoneOffset.UTC));
        when(lease.getObject("lease_expires_at", OffsetDateTime.class)).thenReturn(observed.plusSeconds(30).atOffset(ZoneOffset.UTC));
        when(job.getString("job_type")).thenReturn("vlm_rationale");
        when(job.getString("status")).thenReturn("LEASED");
        when(job.getInt("attempt")).thenReturn(1);
        when(job.getInt("max_attempts")).thenReturn(2);
        when(job.getInt("timeout_ms")).thenReturn(5000);
        when(job.getObject("deadline_at", OffsetDateTime.class)).thenReturn(observed.plusSeconds(5).atOffset(ZoneOffset.UTC));
        doAnswer(call -> {
            RowMapper<Object> mapping = call.getArgument(2);
            return List.of(mapping.mapRow(((String) call.getArgument(0)).contains("FROM job_lease") ? lease : job, 0));
        }).when(jdbc).query(anyString(), any(SqlParameterSource.class), org.mockito.ArgumentMatchers.<RowMapper<Object>>any());
        when(jdbc.queryForList(contains("RETURNING job_type"), any(SqlParameterSource.class), eq(String.class)))
                .thenReturn(List.of("vlm_rationale"));
    }

    @Test
    void 재시도가능_실패는_5초_전이면_다음_시도로_돌린다() {
        assertThat(leases.submit("worker", "job-vlm", failed(false), observed.plusMillis(4999)).status()).isEqualTo(LeaseStatus.Status.ACCEPTED);
        verify(jdbc).update(contains("attempt = attempt + 1"), any(SqlParameterSource.class));
        // 다시 대기열로 돌아가는 상태 변경(JobStateChanged, -369)은 나가도 되고, 작업이 끝났다는 JobFinished 는 나가지 않아야 한다
        verify(events, never()).publishEvent(argThat((Object e) -> e instanceof JobFinished));
    }

    @Test
    void 재시도가능_실패도_정확히_5초면_더_내주지_않는다() {
        assertThat(leases.submit("worker", "job-vlm", failed(false), observed.plusMillis(5000)).status()).isEqualTo(LeaseStatus.Status.ACCEPTED);
        verify(jdbc, never()).update(contains("attempt = attempt + 1"), any(SqlParameterSource.class));
        verify(events).publishEvent(argThat((Object e) -> e instanceof JobFinished f && "TIMED_OUT".equals(f.status())));
    }

    @Test
    void 마지막_시도는_5초_전에도_실패로_끝난다() throws Exception {
        when(job.getInt("attempt")).thenReturn(2);
        leases.submit("worker", "job-vlm", failed(false), observed.plusMillis(1000));
        verify(events).publishEvent(argThat((Object e) -> e instanceof JobFinished f && "FAILED".equals(f.status())));
    }

    @Test
    void 시도미소비_반납도_5초가_지나면_대기열에_돌려놓지_않는다() {
        leases.submit("worker", "job-vlm", failed(true), observed.plusMillis(5001));
        verify(jdbc, never()).update(contains("SET status = 'QUEUED'"), any(SqlParameterSource.class));
        verify(events).publishEvent(argThat((Object e) -> e instanceof JobFinished f && "TIMED_OUT".equals(f.status())));
    }

    private ObjectNode failed(boolean release) {
        ObjectNode p = mapper.createObjectNode().put("lease_id", leaseId.toString()).put("job_type", "vlm_rationale")
                .put("status", "FAILED").put("finished_at", observed.plusMillis(1000).toEpochMilli());
        ObjectNode error = p.putObject("error").put("code", release ? "TIER0_PRIORITY" : "VLM_UNAVAILABLE")
                .put("message", "시험").put("retryable", true);
        if (release) {
            error.put("consume_attempt", false);
        }
        return p;
    }
}
