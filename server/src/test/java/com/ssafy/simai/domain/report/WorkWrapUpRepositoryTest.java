package com.ssafy.simai.domain.report;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;
import static org.assertj.core.api.Assertions.assertThat;
class WorkWrapUpRepositoryTest {
    @Test void countsBoundariesAndElapsedMinutesInAnyOrder() {
        ObjectNode facts = new ObjectMapper().createObjectNode();
        facts.putObject("phase_counts").put("early", 0).put("middle", 0).put("late", 0);
        facts.putNull("first_event_min").putNull("last_event_min");
        for (long t : new long[]{180000, 60000, 120000, 0}) WorkWrapUpRepository.addTiming(facts, 0, 180000, t);
        assertThat(facts.path("phase_counts").path("early").asInt()).isEqualTo(1);
        assertThat(facts.path("phase_counts").path("middle").asInt()).isEqualTo(1);
        assertThat(facts.path("phase_counts").path("late").asInt()).isEqualTo(2);
        assertThat(facts.path("first_event_min").asInt()).isZero();
        assertThat(facts.path("last_event_min").asInt()).isEqualTo(3);
        WorkWrapUpRepository.addTiming(facts, 0, 0, 0);
        assertThat(facts.path("phase_counts").path("early").asInt()).isEqualTo(2);
    }

    @Test void aggregatesOnlyScopedEventsAndRanksZones() throws Exception {
        var jdbc = org.mockito.Mockito.mock(org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate.class);
        var mapper = new ObjectMapper();
        org.mockito.Mockito.doAnswer(invocation -> {
            var handler = (org.springframework.jdbc.core.RowCallbackHandler) invocation.getArgument(2);
            for (int i = 0; i < 4; i++) {
                var rs = org.mockito.Mockito.mock(java.sql.ResultSet.class);
                org.mockito.Mockito.when(rs.getBoolean("in_scope")).thenReturn(i < 3);
                org.mockito.Mockito.when(rs.getBoolean("outside_entry")).thenReturn(i == 3);
                org.mockito.Mockito.when(rs.getBoolean("acked")).thenReturn(i == 1);
                org.mockito.Mockito.when(rs.getString("severity")).thenReturn(i == 2 ? "CAUTION" : "CRITICAL");
                org.mockito.Mockito.when(rs.getString("rule_id")).thenReturn("R1");
                org.mockito.Mockito.when(rs.getString("zone_name")).thenReturn(i == 2 ? "개구부 2" : "개구부 1");
                org.mockito.Mockito.when(rs.getObject("raised_at", java.time.OffsetDateTime.class))
                        .thenReturn(java.time.OffsetDateTime.ofInstant(java.time.Instant.ofEpochMilli(i * 60000L), java.time.ZoneOffset.UTC));
                handler.processRow(rs);
            }
            return null;
        }).when(jdbc).query(org.mockito.ArgumentMatchers.anyString(),
                org.mockito.ArgumentMatchers.any(org.springframework.jdbc.core.namedparam.MapSqlParameterSource.class),
                org.mockito.ArgumentMatchers.any(org.springframework.jdbc.core.RowCallbackHandler.class));
        var work = mapper.createObjectNode().put("actual_started_at", 0).put("actual_ended_at", 180000).putNull("zone_id");
        var facts = new WorkWrapUpRepository(jdbc, mapper).aggregate(work);
        assertThat(facts.path("event_count").asInt()).isEqualTo(3);
        assertThat(facts.path("unacknowledged_critical_count").asInt()).isEqualTo(1);
        assertThat(facts.path("outside_work_entry_count").asInt()).isEqualTo(1);
        assertThat(facts.path("top_zones").get(0).path("name").asString()).isEqualTo("개구부 1");
        assertThat(facts.path("top_zones").get(0).path("count").asInt()).isEqualTo(2);
        assertThat(WorkWrapUpRepository.safeZoneName("잘못\n된 이름")).isEqualTo("현장 전체");
    }
}
