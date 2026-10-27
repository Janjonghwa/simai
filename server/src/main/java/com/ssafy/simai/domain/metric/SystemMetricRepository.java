package com.ssafy.simai.domain.metric;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.core.namedparam.SqlParameterSource;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;

/**
 * system_metric(hypertable, 긴 형식: 한 행에 지표 하나, 명세 3.11·11.2, S15P31A101-70). 키는 (measured_at, source_id, metric, camera_id)
 * 이고 같은 키는 같은 측정이라 다시 쓰지 않는다(health 재전송·같은 시각 샘플). 카메라와 무관한 지표의 camera_id 는 빈 문자열이다.
 */
@Repository
public class SystemMetricRepository {

    private final NamedParameterJdbcTemplate jdbc;

    SystemMetricRepository(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    void insert(List<Row> rows) {
        if (rows.isEmpty()) {
            return;
        }
        SqlParameterSource[] batch = rows.stream().map(row -> new MapSqlParameterSource()
                .addValue("at", Timestamp.from(row.measuredAt()))
                .addValue("source", row.sourceId())
                .addValue("metric", row.metric())
                .addValue("camera", row.cameraId())
                .addValue("value", row.value())
                .addValue("unit", row.unit())
                .addValue("labels", row.labels() == null ? null : row.labels().toString())).toArray(SqlParameterSource[]::new);
        jdbc.batchUpdate("""
                INSERT INTO system_metric (measured_at, source_id, metric, camera_id, value, unit, labels)
                VALUES (:at, :source, :metric, :camera, :value, :unit, CAST(:labels AS jsonb))
                ON CONFLICT DO NOTHING
                """, batch);
    }

    /**
     * 조회 조건에 맞는 행을 늦은 시각부터(상한에서 잘리면 오래된 점이 빠지고 계열은 남는다). latest 면 계열마다 가장 늦은 한 행만.
     * 계열로 묶고 시간 순으로 되돌리는 일은 부르는 쪽이 한다
     */
    List<Map<String, Object>> query(Instant from, Instant to, List<String> metrics, String sourceId, String cameraId, boolean latest,
            int limit) {
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("from", Timestamp.from(from)).addValue("to", Timestamp.from(to))
                .addValue("metrics", metrics.isEmpty() ? null : metrics.toArray(String[]::new))
                .addValue("source", sourceId).addValue("camera", cameraId).addValue("limit", limit);
        String where = """
                measured_at >= :from AND measured_at <= :to
                  AND (CAST(:metrics AS text[]) IS NULL OR metric = ANY(CAST(:metrics AS text[])))
                  AND (CAST(:source AS text) IS NULL OR source_id = :source)
                  AND (CAST(:camera AS text) IS NULL OR camera_id = :camera)
                """;
        String sql = latest
                ? "SELECT DISTINCT ON (source_id, metric, camera_id) measured_at, source_id, metric, camera_id, value, unit,"
                        + " labels::text AS labels FROM system_metric WHERE " + where
                        + " ORDER BY source_id, metric, camera_id, measured_at DESC LIMIT :limit"
                : "SELECT measured_at, source_id, metric, camera_id, value, unit, labels::text AS labels FROM system_metric WHERE "
                        + where + " ORDER BY measured_at DESC, source_id, metric, camera_id LIMIT :limit";
        return jdbc.queryForList(sql, params);
    }

    /** 지표 한 점. labels 는 백분위(p50·p95·p99) 같은 덧붙임 값 */
    public record Row(Instant measuredAt, String sourceId, String metric, String cameraId, double value, String unit, JsonNode labels) {
    }
}
