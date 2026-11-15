package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.global.exception.ErrorCode;
import org.junit.jupiter.api.Test;

/** 세팅 완료 세션 쪽 정본 판정(S15P31A101-688): 새 모델은 정밀 맵까지, 재사용은 World 채택까지, 둘 다 카메라 확인 통과 */
class SetupCompletionGateTest {

    @Test
    void 새_모델은_미리보기로_완료하지_않고_정밀_맵이_있어야_한다() {
        assertThat(SetupCompletionGate.blockedReason("PREVIEW_AND_VLM_DRAFT", "NEW", false, null, null, true))
                .isEqualTo(ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE);
        assertThat(SetupCompletionGate.blockedReason("READY_FOR_ZONES", "NEW", false, null, null, true))
                .isEqualTo(ErrorCode.Setup.SETUP_FINAL_MAP_NOT_READY);
        assertThat(SetupCompletionGate.blockedReason("BAKING_TEXTURE", "NEW", false, null, null, true))
                .isEqualTo(ErrorCode.Setup.SETUP_FINAL_MAP_NOT_READY);
        assertThat(SetupCompletionGate.blockedReason("COMPLETED", "NEW", false, null, "LIVE_WORK_STARTED", true))
                .isEqualTo(ErrorCode.Setup.SETUP_FINAL_MAP_SKIPPED);
        assertThat(SetupCompletionGate.blockedReason("COMPLETED", "NEW", false, "scene-1", null, true)).isNull();
        // 회수 뒤 다시 굽기 APPLIED 가 정밀 맵을 옮겼으면 완료한다
        assertThat(SetupCompletionGate.blockedReason("COMPLETED", "NEW", false, "scene-2", "LIVE_WORK_STARTED", true)).isNull();
        // 모드를 모르면(이전 서버 행) 새 모델로 본다
        assertThat(SetupCompletionGate.blockedReason("READY_FOR_ZONES", null, true, null, null, true))
                .isEqualTo(ErrorCode.Setup.SETUP_FINAL_MAP_NOT_READY);
    }

    @Test
    void 재사용은_World_채택_뒤_READY_부터_완료한다() {
        assertThat(SetupCompletionGate.blockedReason("JOINT_BA_AND_VERIFY", "REUSE", true, null, null, true))
                .isEqualTo(ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE);
        assertThat(SetupCompletionGate.blockedReason("READY_FOR_ZONES", "REUSE", false, null, null, true))
                .isEqualTo(ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE);
        assertThat(SetupCompletionGate.blockedReason("READY_FOR_ZONES", "REUSE", true, null, null, true)).isNull();
        assertThat(SetupCompletionGate.blockedReason("COMPLETED", "REUSE", true, null, null, true)).isNull();
    }

    @Test
    void 실패했거나_카메라_확인을_통과하지_않으면_막는다() {
        assertThat(SetupCompletionGate.blockedReason("FAILED", "NEW", false, "scene-1", null, true))
                .isEqualTo(ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE);
        assertThat(SetupCompletionGate.blockedReason(null, "NEW", false, "scene-1", null, true))
                .isEqualTo(ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE);
        assertThat(SetupCompletionGate.blockedReason("COMPLETED", "NEW", false, "scene-1", null, false))
                .isEqualTo(ErrorCode.Setup.SETUP_CAMERA_CHECK_NOT_PASSED);
        assertThat(SetupCompletionGate.blockedReason("READY_FOR_ZONES", "REUSE", true, null, null, false))
                .isEqualTo(ErrorCode.Setup.SETUP_CAMERA_CHECK_NOT_PASSED);
        // 카메라 확인이 먼저다(통과하지 못한 세팅은 정밀 맵을 기다려도 완료할 수 없다)
        assertThat(SetupCompletionGate.blockedReason("BAKING_TEXTURE", "NEW", false, null, null, false))
                .isEqualTo(ErrorCode.Setup.SETUP_CAMERA_CHECK_NOT_PASSED);
    }

    @Test
    void 고른_맵을_그대로_쓰는_재사용은_World_채택_카메라_확인_없이_완료할_수_있다() {
        // S15P31A101-714: 시작 때 1단계 시점 확인을 통과했고 보정을 새로 만들지 않는다
        assertThat(SetupCompletionGate.blockedReason("COMPLETED", "REUSE", false, null, null, false, true)).isNull();
        assertThat(SetupCompletionGate.blockedReason("FAILED", "REUSE", false, null, null, false, true))
                .isEqualTo(ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE);
        assertThat(SetupCompletionGate.blockedReason("COMPLETED", "NEW", false, null, null, false, true))
                .isEqualTo(ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE);
    }
}
