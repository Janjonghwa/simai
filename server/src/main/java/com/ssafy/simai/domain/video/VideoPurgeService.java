package com.ssafy.simai.domain.video;

import com.ssafy.simai.global.storage.ObjectStorage;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 보관 기간(14일, 11.5)이 지난 상시 영상 조각의 객체를 지운다(S15P31A101-330). 클립 삭제 작업(ClipPurgeService, -72)과 같은 방식이다:
 * 객체 저장소 수명 주기 규칙을 쓰지 않고 이 작업만 지우며, 기록(video_segment 행)은 남기고 deleted_at 을 쓴다.
 * 조각에는 보존 표시가 없다(이벤트와 묶이지 않는다. 사고 구간을 남기려면 이벤트 클립을 보존한다).
 * <p>
 * 조각 하나를 한 트랜잭션에서 행 잠금(FOR UPDATE SKIP LOCKED)을 쥔 채 지운다. 객체를 먼저 지우고, 지우지 못하면 되돌려 deleted_at 을
 * 쓰지 않는다. 실패한 행은 이번 주기에서 빼고 다음 것을 보며, 연달아 실패하면 저장소가 멈춘 것으로 보고 다음 주기로 넘긴다.
 * 클립 삭제 작업과 같은 전용 스레드(clipPurgeTaskScheduler)에서 돌아 1초 주기 작업을 붙잡지 않는다. 한 주기에 지우는 수를 묶어(1,000)
 * 쌓인 뒤에도 클립 삭제 주기를 오래 밀지 않는다(1분 조각·4대면 10분에 40개라 평소엔 수십 개다).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class VideoPurgeService {

    private static final Logger log = LoggerFactory.getLogger(VideoPurgeService.class);
    private static final int MAX_CONSECUTIVE_FAILURES = 3;

    /** 18.4 점검 쿼리와 같은 꼴의 조건: 삭제 예정일이 지났고 아직 지우지 않은 조각 */
    static final String OVERDUE = "deleted_at IS NULL AND delete_after <= :now";

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectStorage storage;
    private final TransactionTemplate transaction;
    private final Clock clock;
    private final Duration orphanGrantAfter;
    private final int maxPerRun;

    VideoPurgeService(NamedParameterJdbcTemplate jdbc, ObjectStorage storage, TransactionTemplate transaction, Clock clock,
            @Value("${simai.video.orphan-grant-after:14d}") Duration orphanGrantAfter,
            @Value("${simai.video.purge-max-per-run:1000}") int maxPerRun) {
        this.jdbc = jdbc;
        this.storage = storage;
        this.transaction = transaction;
        this.clock = clock;
        this.orphanGrantAfter = orphanGrantAfter;
        this.maxPerRun = maxPerRun;
    }

    @Scheduled(scheduler = "clipPurgeTaskScheduler", initialDelayString = "${simai.video.purge-initial-delay:PT2M}",
            fixedDelayString = "${simai.video.purge-interval:PT10M}")
    void run() {
        try {
            purge(clock.instant().truncatedTo(ChronoUnit.MILLIS));
        } catch (RuntimeException e) {
            // 저장소·DB 오류. 오류 문구에는 키가 실릴 수 있어 종류만 남긴다
            log.warn("영상 조각 삭제 작업이 멈춤(다음 주기에 다시): {}", e.getClass().getSimpleName());
        }
    }

    /** 한 주기. 저장소가 설정되지 않았으면 아무것도 하지 않는다(서명도 내지 않으므로 지울 객체도 없다) */
    Result purge(Instant now) {
        if (!storage.configured()) {
            return new Result(0, 0, 0);
        }
        Batch segments = drain("""
                SELECT segment_id::text AS id, object_key FROM video_segment
                WHERE %s AND segment_id::text <> ALL(CAST(:failed AS text[]))
                ORDER BY delete_after LIMIT 1 FOR UPDATE SKIP LOCKED
                """.formatted(OVERDUE), new MapSqlParameterSource("now", utc(now)),
                "UPDATE video_segment SET deleted_at = :now WHERE segment_id = CAST(:id AS uuid)", now);
        // 업로드 주소를 받고 조각 기록이 끝내 오지 않은 객체. 마지막 발급 뒤 보관 기간이 지난 것만 지운다(그 사이 늦게 온 기록은 쓰인 발급이 된다)
        Batch orphans = drain("""
                SELECT grant_id::text AS id, object_key FROM upload_grant
                WHERE purpose = 'VIDEO_SEGMENT' AND used_at IS NULL AND expires_at <= :cutoff
                  AND grant_id::text <> ALL(CAST(:failed AS text[]))
                ORDER BY expires_at LIMIT 1 FOR UPDATE SKIP LOCKED
                """, new MapSqlParameterSource("cutoff", utc(now.minus(orphanGrantAfter))),
                "DELETE FROM upload_grant WHERE grant_id = CAST(:id AS bigint)", now);
        Result result = new Result(segments.done(), orphans.done(), segments.failed() + orphans.failed());
        if (result.failed() > 0) {
            log.warn("영상 조각 삭제 작업: 지운 조각 {}, 쓰이지 않은 업로드 객체 {}, 지우지 못함 {}(다음 주기에 다시)",
                    result.deleted(), result.orphans(), result.failed());
        } else if (result.deleted() + result.orphans() > 0) {
            log.info("영상 조각 삭제 작업: 지운 조각 {}, 쓰이지 않은 업로드 객체 {}", result.deleted(), result.orphans());
        }
        return result;
    }

    /** 18.4 점검: 삭제 예정일이 지났는데 남은 조각 수. 작업이 제때 돌면 0 이다 */
    long overdue(Instant now) {
        return jdbc.queryForObject("SELECT count(*) FROM video_segment WHERE " + OVERDUE,
                new MapSqlParameterSource("now", utc(now)), Long.class);
    }

    /**
     * 한 건씩 트랜잭션 하나로 고르고(행 잠금) 객체를 지운 뒤 after 를 실행한다. 실패한 행은 이번 주기의 제외 목록에 넣고 계속한다.
     * pick 은 제외 목록(:failed, Postgres 배열 문자열)을 받아 id·object_key 한 행을 돌려주는 질의다
     */
    private Batch drain(String pick, MapSqlParameterSource params, String after, Instant now) {
        List<String> failed = new ArrayList<>();
        int done = 0;
        int consecutiveFailures = 0;
        while (done + failed.size() < maxPerRun && consecutiveFailures < MAX_CONSECUTIVE_FAILURES) {
            String[] current = new String[1];
            try {
                Boolean found = transaction.execute(status -> {
                    List<Map<String, Object>> rows = jdbc.queryForList(pick,
                            params.addValue("failed", "{" + String.join(",", failed) + "}"));
                    if (rows.isEmpty()) {
                        return false;
                    }
                    current[0] = (String) rows.getFirst().get("id");
                    // 객체를 먼저 지운다. 지우지 못하면 예외로 되돌려 기록을 바꾸지 않는다
                    storage.delete((String) rows.getFirst().get("object_key"));
                    jdbc.update(after, new MapSqlParameterSource("id", current[0]).addValue("now", utc(now)));
                    return true;
                });
                if (!Boolean.TRUE.equals(found)) {
                    break;
                }
                done++;
                consecutiveFailures = 0;
            } catch (RuntimeException e) {
                if (current[0] == null) {
                    throw e; // 고르기(DB)부터 실패했다. 이번 주기를 끝낸다
                }
                // 키는 남기지 않는다. 식별자와 오류 종류만
                log.warn("영상 조각 삭제 작업: 지우지 못해 이번 주기에서 뺌 id={} error={}", current[0], e.getClass().getSimpleName());
                failed.add(current[0]);
                consecutiveFailures++;
            }
        }
        return new Batch(done, failed.size());
    }

    private record Batch(int done, int failed) {
    }

    /** 한 주기 결과: 지운 조각, 지운 쓰이지 않은 업로드, 지우지 못한 것 */
    record Result(int deleted, int orphans, int failed) {
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }
}
