package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.setup.SetupWaitReason.Basis;
import com.ssafy.simai.domain.setup.SetupWaitReason.Code;
import com.ssafy.simai.domain.setup.SetupWaitReason.Facts;
import com.ssafy.simai.domain.setup.SetupWaitReason.Reason;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** 세팅 대기 이유 고르기(S15P31A101-583). DB 없이 판단 재료만으로 시험한다. */
class SetupWaitReasonTest {

    private static final Instant NOW = Instant.parse("2026-11-10T03:00:00Z");
    private static final Instant CREATED = NOW.minusSeconds(600);
    private static final UUID LEASE = UUID.fromString("00000000-0000-0000-0000-0000000000a1");
    private static final UUID OLD_LEASE = UUID.fromString("00000000-0000-0000-0000-0000000000a0");

    /** 대기열에 있고, 워커 하나가 등록돼 있으나 10분째 대여를 묻지 않고, Tier 0 health 는 없다 */
    private static Facts queued() {
        return new Facts("COLLECTING_BACKGROUND", null, bg(0, 0, 0, 0), "QUEUED", CREATED, null, null, null, null, null,
                1, NOW.minusSeconds(900), null, 0, null, false);
    }

    private static Map<String, Double> bg(double a, double b, double c, double d) {
        Map<String, Double> m = new LinkedHashMap<>();
        m.put("cam_A", a);
        m.put("cam_B", b);
        m.put("cam_C", c);
        m.put("cam_D", d);
        return m;
    }

    private static Facts with(Facts f, String jobErrorCode, String jobErrorMessage, int eligible, Instant lastSeen,
            String otherJob, int healthSamples, Integer lagMax, boolean lagUnknown) {
        return new Facts(f.sessionStatus(), f.progressLeaseId(), f.backgroundPct(), f.jobStatus(), f.jobCreatedAt(),
                jobErrorCode, jobErrorMessage, f.lastLeaseActivityAt(), f.activeLeaseId(), f.activeLeasedAt(), eligible,
                lastSeen, otherJob, healthSamples, lagMax, lagUnknown);
    }

    private static Facts leased(String status, UUID progressLease, Map<String, Double> bg) {
        return new Facts(status, progressLease, bg, "LEASED", CREATED, null, null, NOW.minusSeconds(30), LEASE,
                NOW.minusSeconds(120), 1, NOW.minusSeconds(130), null, 0, null, false);
    }

    @Test
    void 등록된_워커가_없으면_NO_WORKER() {
        Reason r = SetupWaitReason.derive(with(queued(), null, null, 0, null, null, 0, null, false), NOW);
        assertThat(r.code()).isEqualTo(Code.NO_WORKER);
        assertThat(r.since()).isEqualTo(CREATED);
    }

    @Test
    void 워커가_다른_작업을_처리_중이면_그_유형을_싣는다() {
        Reason r = SetupWaitReason.derive(with(queued(), null, null, 1, NOW.minusSeconds(300), "scene_regenerate", 0, null,
                false), NOW);
        assertThat(r.code()).isEqualTo(Code.WORKER_BUSY_OTHER_JOB);
        assertThat(r.otherJobType()).isEqualTo("scene_regenerate");
    }

    @Test
    void 워커가_최근_대여를_물었으면_곧_가져간다() {
        Reason r = SetupWaitReason.derive(with(queued(), null, null, 1, NOW.minusSeconds(10), null, 5, 2000, false), NOW);
        // 대여를 묻는 중이면 Tier 0 지연이 높아도 접수 문이 닫힌 것이 아니다
        assertThat(r.code()).isEqualTo(Code.WAITING_FOR_WORKER);
        assertThat(r.workerLastSeenAt()).isEqualTo(NOW.minusSeconds(10));
    }

    @Test
    void 워커가_Tier0_우선으로_반납했으면_워커_근거로_접수_중지() {
        Facts f = with(queued(), "TIER0_PRIORITY", "Tier 0 우선으로 시작하지 않았다: 표시 파일이 120s 전 것이다", 1,
                NOW.minusSeconds(200), null, 10, 1450, false);
        Reason r = SetupWaitReason.derive(f, NOW);
        assertThat(r.code()).isEqualTo(Code.WORKER_PAUSED_TIER0);
        assertThat(r.basis()).isEqualTo(Basis.WORKER_RELEASE);
        assertThat(r.detail()).contains("Tier 0 우선");
        // 관측한 지연은 참고로 싣는다
        assertThat(r.tier0LagMs()).isEqualTo(1450);
        // 반납은 앞 시도의 실패가 아니다
        assertThat(r.previousErrorCode()).isNull();
    }

    @Test
    void 워커가_묻지_않고_Tier0_가_지연을_모르면_health_근거로_접수_중지를_추정한다() {
        Reason r = SetupWaitReason.derive(with(queued(), null, null, 1, NOW.minusSeconds(900), null, 12, 300, true), NOW);
        assertThat(r.code()).isEqualTo(Code.WORKER_PAUSED_TIER0);
        assertThat(r.basis()).isEqualTo(Basis.TIER0_HEALTH);
        assertThat(r.tier0LagMs()).isEqualTo(300);
    }

    @Test
    void 지연이_높기만_하면_접수_중지로_추정하지_않는다() {
        // site_setup 은 지연 예산 초과만으로는 막히지 않는다(S15P31A101-566). 관측 지연은 참고로만 싣는다
        Reason r = SetupWaitReason.derive(with(queued(), null, null, 1, NOW.minusSeconds(900), null, 12, 1800, false), NOW);
        assertThat(r.code()).isEqualTo(Code.WORKER_NOT_POLLING);
        assertThat(r.basis()).isNull();
        assertThat(r.tier0LagMs()).isEqualTo(1800);
    }

    @Test
    void 근거가_없으면_꾸며내지_않고_워커_무응답() {
        Reason r = SetupWaitReason.derive(queued(), NOW);
        assertThat(r.code()).isEqualTo(Code.WORKER_NOT_POLLING);
        assertThat(r.basis()).isNull();
        assertThat(r.tier0LagMs()).isNull();
        assertThat(r.workerLastSeenAt()).isEqualTo(NOW.minusSeconds(900));
    }

    @Test
    void 재시도_대기면_앞_시도_오류를_싣고_대기_시작은_마지막_대여_움직임() {
        Instant lastActivity = NOW.minusSeconds(40);
        Facts f = new Facts("COLLECTING_BACKGROUND", OLD_LEASE, bg(100, 100, 15, 100), "QUEUED", CREATED,
                "BACKGROUND_INCOMPLETE", "사람 지운 배경 수집 미완료 카메라: ['cam_C']", lastActivity, null, null, 1,
                NOW.minusSeconds(5), null, 0, null, false);
        Reason r = SetupWaitReason.derive(f, NOW);
        assertThat(r.code()).isEqualTo(Code.WAITING_FOR_WORKER);
        assertThat(r.previousErrorCode()).isEqualTo("BACKGROUND_INCOMPLETE");
        assertThat(r.since()).isEqualTo(lastActivity);
    }

    @Test
    void 대여했지만_이_대여의_세팅_하트비트가_없으면_시작_준비() {
        Reason r = SetupWaitReason.derive(leased("COLLECTING_BACKGROUND", OLD_LEASE, bg(100, 100, 15, 100)), NOW);
        // 앞 대여의 수집률을 이 대여 것으로 보이지 않는다
        assertThat(r.code()).isEqualTo(Code.WORKER_STARTING);
        assertThat(r.since()).isEqualTo(NOW.minusSeconds(120));
        assertThat(r.cameras()).isEmpty();
    }

    @Test
    void 배경_수집_중_덜_모인_카메라만_싣는다() {
        Reason r = SetupWaitReason.derive(leased("COLLECTING_BACKGROUND", LEASE, bg(100, 99, 15, 100)), NOW);
        assertThat(r.code()).isEqualTo(Code.BACKGROUND_INCOMPLETE);
        assertThat(r.cameras()).containsExactly(Map.entry("cam_B", 99.0), Map.entry("cam_C", 15.0));
    }

    @Test
    void 정상_진행_끝남_구역_단계는_사유가_없다() {
        assertThat(SetupWaitReason.derive(leased("JOINT_BA_AND_VERIFY", LEASE, bg(100, 100, 100, 100)), NOW)).isNull();
        assertThat(SetupWaitReason.derive(leased("COLLECTING_BACKGROUND", LEASE, bg(100, 100, 100, 100)), NOW)).isNull();
        for (String status : new String[] {"READY_FOR_ZONES", "BAKING_TEXTURE", "COMPLETED", "FAILED"}) {
            Facts f = new Facts(status, null, Map.of(), "QUEUED", CREATED, null, null, null, null, null, 0, null, null, 0,
                    null, false);
            assertThat(SetupWaitReason.derive(f, NOW)).isNull();
            assertThat(SetupWaitReason.mayWait(status)).isFalse();
        }
        Facts terminalJob = new Facts("COLLECTING_BACKGROUND", null, Map.of(), "FAILED", CREATED, null, null, null, null,
                null, 1, null, null, 0, null, false);
        assertThat(SetupWaitReason.derive(terminalJob, NOW)).isNull();
    }

    @Test
    void JSON_은_고정된_칸을_모두_싣는다() {
        Reason r = SetupWaitReason.derive(leased("COLLECTING_BACKGROUND", LEASE, bg(100, 100, 15, 100)), NOW);
        JsonNode json = r.toJson(new ObjectMapper(), NOW);
        assertThat(json.get("code").asString()).isEqualTo("BACKGROUND_INCOMPLETE");
        assertThat(json.get("since_ms").asLong()).isEqualTo(NOW.minusSeconds(120).toEpochMilli());
        assertThat(json.get("waited_s").asLong()).isEqualTo(120);
        for (String field : new String[] {"basis", "detail", "tier0_lag_ms", "worker_last_seen_ms",
                "other_job_type", "previous_error_code"}) {
            assertThat(json.has(field)).as(field).isTrue();
            assertThat(json.get(field).isNull()).as(field).isTrue();
        }
        assertThat(json.get("cameras").size()).isEqualTo(1);
        assertThat(json.get("cameras").get(0).get("camera_id").asString()).isEqualTo("cam_C");
        assertThat(json.get("cameras").get(0).get("collection_pct").asDouble()).isEqualTo(15.0);
    }
}
