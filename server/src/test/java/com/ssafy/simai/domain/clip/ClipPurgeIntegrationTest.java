package com.ssafy.simai.domain.clip;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.reset;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 클립 삭제 작업(S15P31A101-72, 명세 11.5·18.4). 저장소는 가짜(MockitoBean)로 두고 어떤 키를 지우는지와 DB 기록을 본다.
 * 시각은 실제 시각 기준으로 행을 만들고 작업에 같은 시각을 넘긴다.
 */
@SpringBootTest
@ActiveProfiles("test")
class ClipPurgeIntegrationTest {

    @MockitoBean
    ObjectStorage storage;

    @Autowired
    ClipPurgeService purge;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    TransactionTemplate transaction;

    private final String gatewayId = "tier0-purge-" + UUID.randomUUID().toString().substring(0, 8);
    private final List<UUID> events = new ArrayList<>();
    private final Instant now = Instant.now().truncatedTo(ChronoUnit.MILLIS);

    @BeforeEach
    void configured() {
        when(storage.configured()).thenReturn(true);
    }

    @AfterEach
    void cleanUp() {
        for (UUID event : events) {
            jdbc.update("DELETE FROM media_clip_retention WHERE event_id = ?", event);
            jdbc.update("DELETE FROM media_clip WHERE event_id = ?", event);
            jdbc.update("DELETE FROM upload_grant WHERE event_id = ?", event);
            jdbc.update("DELETE FROM risk_event WHERE event_id = ?", event);
        }
    }

    @Test
    void 삭제_예정일이_지났고_보존_표시가_없는_클립만_객체를_지우고_deleted_at_을_남긴다() {
        UUID event = event("HIGH");
        UUID expired = clip(event, "cam-1", now.minus(Duration.ofDays(1)), false);
        UUID held = clip(event, "cam-2", now.minus(Duration.ofDays(1)), true);
        UUID fresh = clip(event, "cam-3", now.plus(Duration.ofDays(1)), false);
        assertThat(purge.overdue(now)).isGreaterThanOrEqualTo(1);

        ClipPurgeService.Result result = purge.purge(now);

        assertThat(result.deleted()).isGreaterThanOrEqualTo(1);
        verify(storage).delete(key(event, expired));
        verify(storage, never()).delete(key(event, held));
        verify(storage, never()).delete(key(event, fresh));
        assertThat(deletedAt(expired)).isEqualTo(now);
        assertThat(deletedAt(held)).isNull();
        assertThat(deletedAt(fresh)).isNull();
        // 18.4 점검 쿼리: 지운 뒤 삭제 예정일이 지난 미삭제 클립은 0건
        assertThat(purge.overdue(now)).isZero();
    }

    @Test
    void 지운_클립은_다음_주기에_다시_지우지_않는다() {
        UUID event = event("HIGH");
        UUID expired = clip(event, "cam-1", now.minus(Duration.ofDays(1)), false);
        purge.purge(now);
        purge.purge(now.plusSeconds(600));

        verify(storage).delete(key(event, expired));
        assertThat(deletedAt(expired)).isEqualTo(now);
    }

    @Test
    void 이벤트가_CRITICAL_로_오른_클립은_지우지_않고_보존_표시와_CRITICAL_AUTO_이력을_남긴다() {
        UUID event = event("CRITICAL");
        UUID clip = clip(event, "cam-1", now.minus(Duration.ofDays(1)), false);

        ClipPurgeService.Result result = purge.purge(now);

        assertThat(result.retained()).isGreaterThanOrEqualTo(1);
        verify(storage, never()).delete(key(event, clip));
        assertThat(deletedAt(clip)).isNull();
        assertThat(jdbc.queryForObject("SELECT retention_hold FROM media_clip WHERE clip_id = ?", Boolean.class, clip)).isTrue();
        assertThat(jdbc.queryForList("""
                SELECT 1 FROM media_clip_retention WHERE clip_id = ? AND reason = 'CRITICAL_AUTO' AND retention_hold
                    AND actor_user_id IS NULL AND gateway_id IS NULL
                """, clip)).hasSize(1);
        assertThat(purge.overdue(now)).isZero();
    }

    @Test
    void 저장소_오류면_deleted_at_을_쓰지_않고_다음_주기에_다시_지운다() {
        UUID event = event("HIGH");
        UUID clip = clip(event, "cam-1", now.minus(Duration.ofDays(1)), false);
        doThrow(new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE)).when(storage).delete(anyString());

        assertThat(purge.purge(now).failed()).isGreaterThanOrEqualTo(1);
        assertThat(deletedAt(clip)).isNull();

        reset(storage);
        when(storage.configured()).thenReturn(true);
        purge.purge(now);
        assertThat(deletedAt(clip)).isEqualTo(now);
    }

    @Test
    void 키_하나가_계속_실패해도_그_뒤_클립과_업로드는_지운다() {
        UUID event = event("HIGH");
        UUID poisoned = clip(event, "cam-1", now.minus(Duration.ofDays(2)), false);
        UUID next = clip(event, "cam-2", now.minus(Duration.ofDays(1)), false);
        String orphan = grant(event, now.minus(Duration.ofDays(15)), null);
        doThrow(new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE)).when(storage).delete(key(event, poisoned));

        ClipPurgeService.Result result = purge.purge(now);

        assertThat(result.failed()).isEqualTo(1);
        assertThat(deletedAt(poisoned)).isNull();
        assertThat(deletedAt(next)).isEqualTo(now);
        verify(storage).delete(orphan);
    }

    @Test
    void 저장소가_설정되지_않았으면_아무것도_하지_않는다() {
        when(storage.configured()).thenReturn(false);
        UUID event = event("HIGH");
        UUID clip = clip(event, "cam-1", now.minus(Duration.ofDays(1)), false);

        assertThat(purge.purge(now)).isEqualTo(new ClipPurgeService.Result(0, 0, 0, 0));
        verify(storage, never()).delete(anyString());
        assertThat(deletedAt(clip)).isNull();
    }

    @Test
    void 다른_트랜잭션이_잠근_클립은_건너뛰고_보존_표시가_붙으면_지우지_않는다() throws Exception {
        UUID event = event("HIGH");
        UUID clip = clip(event, "cam-1", now.minus(Duration.ofDays(1)), false);
        CountDownLatch locked = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        // 관리자 보존 표시 붙이기(ClipService.setRetention)처럼 행을 잠그고 보존 표시를 켠다
        Thread admin = new Thread(() -> transaction.executeWithoutResult(status -> {
            jdbc.queryForList("SELECT 1 FROM media_clip WHERE clip_id = ? FOR UPDATE", clip);
            jdbc.update("UPDATE media_clip SET retention_hold = true WHERE clip_id = ?", clip);
            locked.countDown();
            try {
                release.await(10, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }));
        admin.start();
        assertThat(locked.await(10, TimeUnit.SECONDS)).isTrue();

        long started = System.nanoTime();
        purge.purge(now);
        // 잠긴 행을 기다리지 않고 건너뛴다(SKIP LOCKED). 기다렸다면 관리자 쪽이 10초 뒤에야 풀린다
        assertThat(Duration.ofNanos(System.nanoTime() - started)).isLessThan(Duration.ofSeconds(5));
        release.countDown();
        admin.join(10_000);

        verify(storage, never()).delete(key(event, clip));
        assertThat(deletedAt(clip)).isNull();
        // 보존 표시가 붙었으니 다음 주기에도 지우지 않는다
        purge.purge(now);
        verify(storage, never()).delete(key(event, clip));
    }

    @Test
    void 클립_기록이_오지_않은_업로드는_발급_만료_14일_뒤에_객체와_발급을_지운다() {
        UUID event = event("HIGH");
        String stale = grant(event, now.minus(Duration.ofDays(15)), null);
        String recent = grant(event, now.minus(Duration.ofDays(13)), null);
        String used = grant(event, now.minus(Duration.ofDays(15)), now.minus(Duration.ofDays(15)));

        ClipPurgeService.Result result = purge.purge(now);

        assertThat(result.orphans()).isGreaterThanOrEqualTo(1);
        verify(storage).delete(stale);
        verify(storage, never()).delete(recent);
        verify(storage, never()).delete(used);
        assertThat(jdbc.queryForList("SELECT object_key FROM upload_grant WHERE event_id = ?", String.class, event))
                .containsExactlyInAnyOrder(recent, used);
    }

    @Test
    void CRITICAL_이벤트의_쓰이지_않은_업로드는_지우지_않는다() {
        UUID event = event("CRITICAL");
        String stale = grant(event, now.minus(Duration.ofDays(15)), null);

        purge.purge(now);

        verify(storage, never()).delete(stale);
        assertThat(jdbc.queryForList("SELECT object_key FROM upload_grant WHERE event_id = ?", String.class, event)).containsExactly(stale);
    }

    private UUID event(String severity) {
        UUID event = UUID.randomUUID();
        events.add(event);
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity, confidence,
                    needs_check, world_ids, metrics, resume_count, raised_at, retention_hold)
                VALUES (?, 'LIVE', ?, 'R2', 1, 'RAISED', 'VERIFIED', ?, 'HIGH', false, '{}', '{}'::jsonb, 0, now(), false)
                """, event, gatewayId, severity);
        return event;
    }

    /** 클립 등급은 HIGH(클립을 보낸 뒤 이벤트가 CRITICAL 로 오른 경우를 이벤트 등급으로 만든다) */
    private UUID clip(UUID event, String camera, Instant deleteAfter, boolean hold) {
        UUID clipId = UUID.randomUUID();
        OffsetDateTime created = utc(deleteAfter.minus(Duration.ofDays(14)));
        jdbc.update("""
                INSERT INTO media_clip (clip_id, event_id, gateway_id, event_sequence, resume_count, rule_id, severity, source, camera_id,
                    stream_epochs, anchor_capture_at, requested_start_at, requested_end_at, clip_start_capture_at, clip_end_capture_at,
                    frame_count, starts_with_keyframe, truncated_start, truncated_end, has_gaps, content_type, container, size_bytes,
                    sha256, face_redaction, clip_created_at, delete_after, retention_hold, object_key, uploaded_at, received_at)
                VALUES (?, ?, ?, 1, 0, 'R2', 'HIGH', 'LIVE', ?, '{1}', ?, ?, ?, ?, ?, 300, true, false, false, false, 'video/h264',
                    'h264-annexb', 1024, ?, 'NONE', ?, ?, ?, ?, ?, ?)
                """, clipId, event, gatewayId, camera, created, created, created, created, created,
                "sha256:" + "0".repeat(64), created, utc(deleteAfter), hold, key(event, clipId), created, created);
        return clipId;
    }

    private String grant(UUID event, Instant expiresAt, Instant usedAt) {
        String key = "event-clips/" + event + "/" + UUID.randomUUID() + ".h264";
        jdbc.update("""
                INSERT INTO upload_grant (object_key, purpose, requester, content_type, size_bytes, sha256, event_id, issued_at, expires_at,
                    used_at)
                VALUES (?, 'EVENT_CLIP', 'TIER0', 'video/h264', 1024, ?, ?, ?, ?, ?)
                """, key, "0".repeat(64), event, utc(expiresAt.minus(Duration.ofMinutes(5))), utc(expiresAt),
                usedAt == null ? null : utc(usedAt));
        return key;
    }

    private static String key(UUID event, UUID clipId) {
        return "event-clips/" + event + "/" + clipId + ".h264";
    }

    private Instant deletedAt(UUID clipId) {
        OffsetDateTime t = jdbc.queryForObject("SELECT deleted_at FROM media_clip WHERE clip_id = ?", OffsetDateTime.class, clipId);
        return t == null ? null : t.toInstant();
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }
}
