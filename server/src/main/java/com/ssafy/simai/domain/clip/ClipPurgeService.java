package com.ssafy.simai.domain.clip;

import com.ssafy.simai.global.storage.ObjectStorage;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 보관 기간이 지난 이벤트 클립의 객체를 지운다(EC2 클립 삭제 작업, 명세 11.5·18.4, S15P31A101-72).
 * 객체 저장소 수명 주기 규칙은 DB 의 보존 표시를 모르므로 쓰지 않고, 이 작업만 지운다. 기록(media_clip 행)은 남기고 deleted_at 을 쓴다.
 * <p>
 * 클립 하나를 한 트랜잭션에서 행 잠금(FOR UPDATE)을 쥔 채 지운다. 관리자 보존 표시(ClipService)·Tier 0 보존 기록도 같은 행을 잠그므로,
 * 고른 뒤 지우기 전에 보존 표시가 붙어 보존해야 할 객체를 지우는 일이 없다. 다른 작업이 잠근 행은 건너뛰고 다음 주기에 본다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ClipPurgeService {

    private static final Logger log = LoggerFactory.getLogger(ClipPurgeService.class);
    /** 연달아 이만큼 지우지 못하면 저장소가 멈춘 것으로 본다 */
    private static final int MAX_CONSECUTIVE_FAILURES = 3;

    /** 18.4 점검 쿼리와 같은 조건: 삭제 예정일이 지났고 보존 표시가 없고 아직 지우지 않은 클립 */
    static final String OVERDUE = "c.deleted_at IS NULL AND NOT c.retention_hold AND c.delete_after <= :now";

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectStorage storage;
    private final TransactionTemplate transaction;
    private final Duration orphanGrantAfter;
    private final int maxPerRun;

    public ClipPurgeService(NamedParameterJdbcTemplate jdbc, ObjectStorage storage, TransactionTemplate transaction,
            @Value("${simai.clip.orphan-grant-after:14d}") Duration orphanGrantAfter,
            @Value("${simai.clip.purge-max-per-run:1000}") int maxPerRun) {
        this.jdbc = jdbc;
        this.storage = storage;
        this.transaction = transaction;
        this.orphanGrantAfter = orphanGrantAfter;
        this.maxPerRun = maxPerRun;
    }

    /**
     * 한 주기. 저장소가 설정되지 않았으면 아무것도 하지 않는다(서명도 내지 않으므로 지울 객체도 없다).
     * 객체 하나를 지우지 못하면 그 행은 이번 주기에서 빼고 다음 것을 본다(키 하나의 영구 오류가 나머지를 막지 않게).
     * 연달아 MAX_CONSECUTIVE_FAILURES 번 실패하면 저장소가 멈춘 것으로 보고 이번 주기를 끝낸다.
     */
    public Result purge(Instant now) {
        if (!storage.configured()) {
            return new Result(0, 0, 0, 0);
        }
        // 보존 표시는 저장소를 부르지 않으므로 먼저 붙인다(저장소 오류가 나도 붙는다)
        int retained = retainCritical(now);
        Batch clips = purgeClips(now);
        Batch orphans = purgeOrphanGrants(now);
        Result result = new Result(clips.done(), retained, orphans.done(), clips.failed() + orphans.failed());
        if (result.failed() > 0) {
            log.warn("클립 삭제 작업: 지운 클립 {}, CRITICAL 이벤트라 보존 표시 {}, 쓰이지 않은 업로드 객체 {}, 지우지 못함 {}(다음 주기에 다시)",
                    result.deleted(), result.retained(), result.orphans(), result.failed());
        } else if (result.deleted() + result.retained() + result.orphans() > 0) {
            log.info("클립 삭제 작업: 지운 클립 {}, CRITICAL 이벤트라 보존 표시 {}, 쓰이지 않은 업로드 객체 {}",
                    result.deleted(), result.retained(), result.orphans());
        }
        return result;
    }

    /** 18.4 점검: 삭제 예정일이 지났는데 남은 클립 수. 작업이 제때 돌면 0 이다 */
    public long overdue(Instant now) {
        return jdbc.queryForObject("SELECT count(*) FROM media_clip c WHERE " + OVERDUE,
                new MapSqlParameterSource("now", utc(now)), Long.class);
    }

    /**
     * 클립 객체를 지우고 deleted_at 을 쓴다. 클립을 보낸 뒤 이벤트가 CRITICAL 로 올랐는데 Tier 0 보존 기록이 오지 않은 클립은
     * 지우지 않는다(retainCritical 이 보존 표시를 붙인다). 이벤트 행도 FOR SHARE 로 잠가, 고른 뒤 지우는 사이에 이벤트가 CRITICAL 로
     * 오르지 못하게 한다(오르는 쪽이 기다린다). 잠긴 클립·이벤트는 건너뛰고 다음 주기에 본다.
     */
    private Batch purgeClips(Instant now) {
        return drain(failed -> {
            List<Map<String, Object>> rows = jdbc.queryForList("""
                    SELECT c.clip_id::text AS id, c.object_key FROM media_clip c JOIN risk_event e ON e.event_id = c.event_id
                    WHERE %s AND e.severity <> 'CRITICAL' AND c.clip_id::text <> ALL(CAST(:failed AS text[]))
                    ORDER BY c.delete_after LIMIT 1
                    FOR UPDATE OF c SKIP LOCKED FOR SHARE OF e SKIP LOCKED
                    """.formatted(OVERDUE), new MapSqlParameterSource("now", utc(now)).addValue("failed", failed));
            if (rows.isEmpty()) {
                return null;
            }
            String clipId = (String) rows.getFirst().get("id");
            return new Item(clipId, () -> {
                // 객체를 먼저 지운다. 지우지 못하면 예외로 되돌려 deleted_at 을 쓰지 않는다
                storage.delete((String) rows.getFirst().get("object_key"));
                jdbc.update("UPDATE media_clip SET deleted_at = :now WHERE clip_id = CAST(:c AS uuid)",
                        new MapSqlParameterSource("c", clipId).addValue("now", utc(now)));
            });
        }, "clip_id");
    }

    /**
     * 삭제 예정일이 지났지만 이벤트가 지금 CRITICAL 인 클립에 보존 표시를 붙이고 CRITICAL_AUTO 이력을 남긴다(11.5 "CRITICAL 이벤트 연결분은
     * 자동 보존"). 보통은 Tier 0 의 media_clip_retention 이 먼저 붙이고, 그 기록이 오지 않았을 때만 여기서 붙는다. 이력의 Tier 0 칸(gateway_id·
     * source)과 관리자 칸은 비워 EC2 가 붙인 것임을 남긴다.
     */
    private int retainCritical(Instant now) {
        Integer count = transaction.execute(status -> {
            List<Map<String, Object>> rows = jdbc.queryForList("""
                    SELECT c.clip_id, c.event_id FROM media_clip c JOIN risk_event e ON e.event_id = c.event_id
                    WHERE %s AND e.severity = 'CRITICAL'
                    ORDER BY c.delete_after LIMIT :limit FOR UPDATE OF c SKIP LOCKED
                    """.formatted(OVERDUE), new MapSqlParameterSource("now", utc(now)).addValue("limit", maxPerRun));
            for (Map<String, Object> row : rows) {
                MapSqlParameterSource params = new MapSqlParameterSource("c", row.get("clip_id"))
                        .addValue("e", row.get("event_id")).addValue("now", utc(now));
                jdbc.update("UPDATE media_clip SET retention_hold = true WHERE clip_id = :c", params);
                jdbc.update("""
                        INSERT INTO media_clip_retention (clip_id, event_id, severity, retention_hold, reason, issued_at)
                        VALUES (:c, :e, 'CRITICAL', true, 'CRITICAL_AUTO', :now)
                        """, params);
            }
            return rows.size();
        });
        return count == null ? 0 : count;
    }

    /**
     * 클립 업로드 주소를 받고 클립 기록(media_clip)이 끝내 오지 않은 객체를 지우고 발급 행도 지운다(server-object-storage.md 남은 일).
     * 마지막 발급 뒤 클립 보관 기간(14일)이 지난 것만 본다: 그 사이에 기록이 오면 쓰인 발급이 되고, 늦게 온 기록도 이미 삭제 예정일이 지났다.
     * 발급을 다시 받는 요청(UploadSignService)과 기록 수신(MediaClipIngestor)도 이 행을 잠그므로 지우는 동안 새 업로드·기록과 엇갈리지 않는다.
     */
    private Batch purgeOrphanGrants(Instant now) {
        OffsetDateTime cutoff = utc(now.minus(orphanGrantAfter));
        return drain(failed -> {
            // 이벤트가 CRITICAL 이면 늦게 올 클립이 자동 보존 대상이라 지우지 않는다(발표 후 일괄 파기 때 지운다)
            List<Map<String, Object>> rows = jdbc.queryForList("""
                    SELECT g.grant_id::text AS id, g.object_key FROM upload_grant g LEFT JOIN risk_event e ON e.event_id = g.event_id
                    WHERE g.purpose = 'EVENT_CLIP' AND g.used_at IS NULL AND g.expires_at <= :cutoff
                      AND e.severity IS DISTINCT FROM 'CRITICAL' AND g.grant_id::text <> ALL(CAST(:failed AS text[]))
                    ORDER BY g.expires_at LIMIT 1 FOR UPDATE OF g SKIP LOCKED
                    """, new MapSqlParameterSource("cutoff", cutoff).addValue("failed", failed));
            if (rows.isEmpty()) {
                return null;
            }
            String grantId = (String) rows.getFirst().get("id");
            return new Item(grantId, () -> {
                storage.delete((String) rows.getFirst().get("object_key"));
                jdbc.update("DELETE FROM upload_grant WHERE grant_id = CAST(:g AS bigint)", new MapSqlParameterSource("g", grantId));
            });
        }, "grant_id");
    }

    /**
     * 한 건씩 트랜잭션 하나로 고르고(행 잠금) 지운다. 실패한 행은 이번 주기의 제외 목록에 넣고 계속한다.
     * pick 은 제외 목록(Postgres 배열 문자열)을 받아 다음 행을 잠그고 지울 일을 돌려준다. 없으면 null.
     */
    private Batch drain(Function<String, Item> pick, String idName) {
        List<String> failed = new ArrayList<>();
        int done = 0;
        int consecutiveFailures = 0;
        while (done + failed.size() < maxPerRun && consecutiveFailures < MAX_CONSECUTIVE_FAILURES) {
            AtomicReference<String> current = new AtomicReference<>();
            try {
                Boolean found = transaction.execute(status -> {
                    Item item = pick.apply("{" + String.join(",", failed) + "}");
                    if (item == null) {
                        return false;
                    }
                    current.set(item.id());
                    item.work().run();
                    return true;
                });
                if (!Boolean.TRUE.equals(found)) {
                    break;
                }
                done++;
                consecutiveFailures = 0;
            } catch (RuntimeException e) {
                if (current.get() == null) {
                    throw e; // 고르기(DB)부터 실패했다. 이번 주기를 끝낸다
                }
                // 키는 남기지 않는다. 식별자와 오류 종류만(저장소 상태 코드는 ObjectStorage 가 남겼다)
                log.warn("클립 삭제 작업: 지우지 못해 이번 주기에서 뺌 {}={} error={}", idName, current.get(), e.getClass().getSimpleName());
                failed.add(current.get());
                consecutiveFailures++;
            }
        }
        return new Batch(done, failed.size());
    }

    private record Item(String id, Runnable work) {
    }

    private record Batch(int done, int failed) {
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }

    /** 한 주기 결과: 지운 클립, 보존 표시를 붙인 CRITICAL 클립, 지운 쓰이지 않은 업로드, 지우지 못한 것 */
    public record Result(int deleted, int retained, int orphans, int failed) {
    }
}
