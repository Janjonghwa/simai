package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.UUID;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** 카메라 확인 걷기 창 단계·요청 판단(S15P31A101-687) */
class SetupCameraCheckTest {

    private final ObjectMapper mapper = JsonMapper.builder().build();
    private final UUID lease = UUID.fromString("0192a6f0-7c1e-7b3a-9f2d-0000000001b1");

    private ObjectNode check(String status, int attempt, boolean userWait) {
        ObjectNode c = mapper.createObjectNode().put("status", status);
        if (attempt > 0) {
            c.putObject("attempt").put("no", attempt).put("id", "l-" + attempt);
        }
        if (userWait) {
            c.putObject("user_wait").put("until_ms", 1L).put("wait_s", 10.0);
        }
        return c;
    }

    private JsonNode request(int attempt, UUID leaseId) {
        return mapper.createObjectNode().put("attempt", attempt).put("lease_id", leaseId.toString());
    }

    @Test
    void 단계는_요청_대기_시도_결과를_한_값으로_고른다() {
        assertThat(SetupCameraCheck.phase(true, null, null, lease)).isNull();
        assertThat(SetupCameraCheck.phase(true, check("AWAITING_START", 0, true), null, lease))
                .isEqualTo(SetupCameraCheck.Phase.AWAITING_START);
        assertThat(SetupCameraCheck.phase(true, check("AWAITING_START", 0, true), request(1, lease), lease))
                .isEqualTo(SetupCameraCheck.Phase.STARTING);
        assertThat(SetupCameraCheck.phase(true, check("MEASURING", 1, false), request(1, lease), lease))
                .isEqualTo(SetupCameraCheck.Phase.WALKING);
        assertThat(SetupCameraCheck.phase(true, check("INCOMPLETE", 1, true), request(1, lease), lease))
                .isEqualTo(SetupCameraCheck.Phase.RETRY_WAIT);
        assertThat(SetupCameraCheck.phase(true, check("PASSED", 2, false), request(2, lease), lease))
                .isEqualTo(SetupCameraCheck.Phase.PASSED);
        // 옛 워커(시도·대기 없음)의 실패, 닫힌 세션은 다시 확인을 받지 않는다
        assertThat(SetupCameraCheck.phase(true, check("FAILED", 0, false), null, lease)).isEqualTo(SetupCameraCheck.Phase.FAILED);
        assertThat(SetupCameraCheck.phase(false, check("INCOMPLETE", 1, true), null, lease))
                .isEqualTo(SetupCameraCheck.Phase.FAILED);
    }

    @Test
    void 다른_대여에_한_요청은_이_대여의_요청이_아니다() {
        UUID other = UUID.fromString("0192a6f0-7c1e-7b3a-9f2d-0000000002b2");
        assertThat(SetupCameraCheck.requestFor(request(3, other), lease)).isZero();
        assertThat(SetupCameraCheck.pending(request(3, other), check("AWAITING_START", 0, true), lease)).isFalse();
        assertThat(SetupCameraCheck.phase(true, check("AWAITING_START", 0, true), request(3, other), lease))
                .isEqualTo(SetupCameraCheck.Phase.AWAITING_START);
    }

    @Test
    void 끝난_시도만_기록하고_사유를_남긴다() {
        assertThat(SetupCameraCheck.attemptRecord(mapper, check("MEASURING", 1, false), lease)).isNull();
        assertThat(SetupCameraCheck.attemptRecord(mapper, check("FAILED", 0, false), lease)).isNull();
        ObjectNode failed = check("FAILED", 2, true);
        failed.putArray("problems").addObject().put("reason", "REPROJECTION_HIGH");
        JsonNode record = mapper.readTree(SetupCameraCheck.attemptRecord(mapper, failed, lease));
        assertThat(record.path("no").intValue()).isEqualTo(2);
        assertThat(record.path("lease_id").asString()).isEqualTo(lease.toString());
        assertThat(record.path("reasons").get(0).asString()).isEqualTo("REPROJECTION_HIGH");
    }

    @Test
    void 재생_시험_건너뜀은_허용될_때만_관문을_통과하고_통과로는_세지_않는다() {
        // S15P31A101-736: SKIPPED_REPLAY 는 스위치가 켜져 있고 입력이 모두 녹화 파일인 동안(허용)만 관문 통과, 재사용 후보(passed)는 아니다
        assertThat(SetupCameraCheck.satisfied("SKIPPED_REPLAY", "cal-1", "cal-1", true)).isTrue();
        assertThat(SetupCameraCheck.satisfied("SKIPPED_REPLAY", "cal-1", "cal-1", false)).as("스위치 끔 또는 실제 카메라").isFalse();
        assertThat(SetupCameraCheck.satisfied("SKIPPED_REPLAY", "cal-0", "cal-1", true)).as("다른 보정").isFalse();
        assertThat(SetupCameraCheck.satisfied("SKIPPED_REPLAY", "cal-1", null, true)).isFalse();
        assertThat(SetupCameraCheck.satisfied("PASSED", "cal-1", "cal-1", false)).as("걷기 통과는 그대로").isTrue();
        assertThat(SetupCameraCheck.satisfied("FAILED", "cal-1", "cal-1", true)).isFalse();
        assertThat(SetupCameraCheck.passed("SKIPPED_REPLAY", "cal-1", "cal-1")).isFalse();
        assertThat(SetupCameraCheck.phase(true, check("SKIPPED_REPLAY", 0, false), null, lease))
                .isEqualTo(SetupCameraCheck.Phase.SKIPPED_REPLAY);
        assertThat(SetupCameraCheck.phase(false, check("SKIPPED_REPLAY", 0, false), null, lease))
                .isEqualTo(SetupCameraCheck.Phase.SKIPPED_REPLAY);
        assertThat(SetupCameraCheck.awaitingUser(check("SKIPPED_REPLAY", 0, false))).isFalse();
        ObjectNode snapshot = mapper.createObjectNode();
        snapshot.set("camera_check", SetupCameraCheckFixtures.check(mapper, "cal-1", "SKIPPED_REPLAY"));
        assertThat(SetupCameraCheck.cameraCheck(mapper, snapshot)).as("세션에 저장한다").isNotNull();
    }
}
