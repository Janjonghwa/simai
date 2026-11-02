package com.ssafy.simai.domain.cctv;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;

/**
 * 원격 CCTV 송출 사용 기록(V24 cctv_remote_usage, 추가만, AGENTS 규칙 6). 월 누적은 기록 합이다. 영상은 저장하지 않는다.
 * 수신 연결이 현장 노트북(LAPTOP)인지 보는 기기 종류 조회도 여기 둔다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class CctvUsageRepository {

    private final NamedParameterJdbcTemplate jdbc;

    CctvUsageRepository(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    void insert(Instant periodStart, Instant periodEnd, long sentBytes, int peakViewers, Instant recordedAt) {
        jdbc.update("""
                INSERT INTO cctv_remote_usage (period_start, period_end, sent_bytes, peak_viewers, recorded_at)
                VALUES (:start, :end, :bytes, :viewers, :recordedAt)
                """, new MapSqlParameterSource()
                .addValue("start", utc(periodStart)).addValue("end", utc(periodEnd)).addValue("bytes", sentBytes)
                .addValue("viewers", peakViewers).addValue("recordedAt", utc(recordedAt)));
    }

    /** [from, to) 에 시작한 기록의 송출 바이트 합. */
    long sentBytes(Instant from, Instant to) {
        Long sum = jdbc.queryForObject("""
                SELECT COALESCE(SUM(sent_bytes), 0) FROM cctv_remote_usage WHERE period_start >= :from AND period_start < :to
                """, new MapSqlParameterSource().addValue("from", utc(from)).addValue("to", utc(to)), Long.class);
        return sum == null ? 0 : sum;
    }

    String deviceKind(String deviceId) {
        return jdbc.queryForList("SELECT kind FROM device WHERE device_id = :id", new MapSqlParameterSource("id", deviceId),
                String.class).stream().findFirst().orElse(null);
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }
}
