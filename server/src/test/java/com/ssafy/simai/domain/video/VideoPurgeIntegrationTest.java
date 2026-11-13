package com.ssafy.simai.domain.video;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.reset;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.ssafy.simai.global.storage.ObjectStorage;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

/**
 * 상시 영상 조각 삭제 작업(S15P31A101-330, 명세 11.5). 저장소는 가짜(MockitoBean)로 두고 어떤 키를 지우는지와 DB 기록을 본다.
 * 다른 시험의 행이 같은 주기에 지워질 수 있어 이 시험의 키만 확인한다.
 */
@SpringBootTest
@ActiveProfiles("test")
class VideoPurgeIntegrationTest {

    @MockitoBean
    ObjectStorage storage;

    @Autowired
    VideoPurgeService purge;

    @Autowired
    JdbcTemplate jdbc;

    private final String gatewayId = "tier0-vpurge-" + UUID.randomUUID().toString().substring(0, 8);
    private final Instant now = Instant.now().truncatedTo(ChronoUnit.MILLIS);

    @BeforeEach
    void configured() {
        when(storage.configured()).thenReturn(true);
    }

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM upload_grant WHERE object_key LIKE ?", "video-segments/" + gatewayId + "-%");
        jdbc.update("DELETE FROM upload_grant WHERE object_key LIKE ?", "recording-segments/" + gatewayId + "-%");
        jdbc.update("DELETE FROM device WHERE device_id = ?", gatewayId);
        jdbc.update("DELETE FROM video_segment WHERE gateway_id = ?", gatewayId);
    }

    @Test
    void 삭제_예정일이_지난_조각만_객체를_지우고_deleted_at_을_남긴다() {
        String overdue = segment(now.minusSeconds(1));
        String fresh = segment(now.plus(Duration.ofDays(1)));

        purge.purge(now);

        verify(storage).delete(overdue);
        verify(storage, never()).delete(fresh);
        assertThat(deletedAt(overdue)).isEqualTo(Timestamp.from(now));
        assertThat(deletedAt(fresh)).isNull();
        // 지운 조각은 다음 주기에 다시 지우지 않는다
        purge.purge(now.plusSeconds(60));
        verify(storage).delete(overdue);
    }

    @Test
    void 저장소_오류면_deleted_at_을_쓰지_않고_다음_주기에_다시_지운다() {
        String key = segment(now.minusSeconds(1));
        doThrow(new IllegalStateException("저장소 멈춤")).when(storage).delete(eq(key));

        purge.purge(now);
        assertThat(deletedAt(key)).isNull();

        reset(storage);
        when(storage.configured()).thenReturn(true);
        purge.purge(now);
        assertThat(deletedAt(key)).isEqualTo(Timestamp.from(now));
    }

    @Test
    void 조각_기록이_오지_않은_업로드는_발급_만료_14일_뒤에_객체와_발급을_지운다() {
        String old = grant(now.minus(Duration.ofDays(15)));
        String recent = grant(now.minus(Duration.ofDays(1)));

        purge.purge(now);

        verify(storage).delete(old);
        verify(storage, never()).delete(recent);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM upload_grant WHERE object_key = ?", Integer.class, old)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM upload_grant WHERE object_key = ?", Integer.class, recent)).isOne();
    }

    @Test
    void 게이트웨이_녹화_조각_발급도_기록이_오지_않으면_만료_14일_뒤에_지운다() {
        // S15P31A101-668: RECORDING_SEGMENT 는 기기가 받는다. 올린 뒤 등록이 끝내 오지 않은 객체도 같은 규칙으로 지운다
        String old = recordingGrant(now.minus(Duration.ofDays(15)));
        String recent = recordingGrant(now.minus(Duration.ofDays(1)));

        purge.purge(now);

        verify(storage).delete(old);
        verify(storage, never()).delete(recent);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM upload_grant WHERE object_key = ?", Integer.class, old)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM upload_grant WHERE object_key = ?", Integer.class, recent)).isOne();
    }

    @Test
    void 저장소가_설정되지_않았으면_아무것도_하지_않는다() {
        when(storage.configured()).thenReturn(false);
        String key = segment(now.minusSeconds(1));

        assertThat(purge.purge(now)).isEqualTo(new VideoPurgeService.Result(0, 0, 0));
        verify(storage, never()).delete(anyString());
        assertThat(deletedAt(key)).isNull();
    }

    /** 조각 한 행(키는 이 시험만의 앞부분). 키를 돌려준다 */
    private String segment(Instant deleteAfter) {
        UUID id = UUID.randomUUID();
        String key = "video-segments/" + gatewayId + "-" + id + ".h264";
        Timestamp t = Timestamp.from(now.minus(Duration.ofDays(14)));
        jdbc.update("""
                INSERT INTO video_segment (segment_id, gateway_id, camera_id, stream_epochs, segment_start_capture_at,
                    segment_end_capture_at, frame_count, starts_with_keyframe, has_gaps, content_type, container, size_bytes, sha256,
                    face_redaction, segment_created_at, delete_after, object_key, uploaded_at, received_at)
                VALUES (?, ?, 'cam1', '{1}', ?, ?, 10, true, false, 'video/h264', 'h264-annexb', 10, 'sha256:x', 'NONE', ?, ?, ?, ?, ?)
                """, id, gatewayId, t, t, t, Timestamp.from(deleteAfter), key, t, t);
        return key;
    }

    /** 쓰이지 않은 VIDEO_SEGMENT 발급 한 행 */
    private String grant(Instant expiresAt) {
        String key = "video-segments/" + gatewayId + "-" + UUID.randomUUID() + ".h264";
        jdbc.update("""
                INSERT INTO upload_grant (object_key, purpose, requester, content_type, size_bytes, sha256, issued_at, expires_at)
                VALUES (?, 'VIDEO_SEGMENT', 'TIER0', 'video/h264', 10, ?, ?, ?)
                """, key, "b".repeat(64), Timestamp.from(expiresAt.minusSeconds(300)), Timestamp.from(expiresAt));
        return key;
    }

    /** 쓰이지 않은 RECORDING_SEGMENT 발급 한 행(기기) */
    private String recordingGrant(Instant expiresAt) {
        String key = "recording-segments/" + gatewayId + "-" + UUID.randomUUID() + ".mp4";
        jdbc.update("INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (?, 'LAPTOP', 'x', now()) ON CONFLICT DO NOTHING",
                gatewayId);
        jdbc.update("""
                INSERT INTO upload_grant (object_key, purpose, requester, requested_by_device, content_type, size_bytes, sha256,
                    issued_at, expires_at)
                VALUES (?, 'RECORDING_SEGMENT', 'DEVICE', ?, 'video/mp4', 10, ?, ?, ?)
                """, key, gatewayId, "b".repeat(64), Timestamp.from(expiresAt.minusSeconds(300)), Timestamp.from(expiresAt));
        return key;
    }

    private Timestamp deletedAt(String key) {
        return jdbc.queryForObject("SELECT deleted_at FROM video_segment WHERE object_key = ?", Timestamp.class, key);
    }
}
