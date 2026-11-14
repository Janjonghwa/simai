package com.ssafy.simai.domain.setup;

import com.ssafy.simai.global.exception.ErrorCode;
import java.util.Set;

/**
 * 세팅 완료(C17)의 세션 쪽 정본 판정(S15P31A101-688, docs/design/S15P31A101-688-new-complete-time-budget.md).
 * 완료(POST .../complete)와 세션 조회의 {@code complete_blocked_reason} 이 같은 판정을 쓴다(화면은 그 값을 그대로 따른다).
 * <ul>
 *   <li>새 모델(NEW): 이 세션의 정밀 맵({@code scene_version}, 정식 굽기 또는 다시 굽기 APPLIED)이 있어야 한다. 미리보기로는 완료하지
 *       않는다(미리보기로 완료·작업 시작하면 남은 굽기가 LIVE 회수돼 시연 맵이 미리보기 품질로 남는다).
 *   <li>이전 모델 재사용(REUSE): 미리보기 단계 이후(READY_FOR_ZONES 부터)이고 World 다시 맞추기가 채택됐다(검증된 기존 맵).
 *   <li>둘 다 카메라 확인 PASSED 이고 그 확인의 보정이 세션 보정과 같다(-646·-687).
 * </ul>
 * 작업 쪽 조건(오늘·PLANNED·관제 중·걸린 세션)과 구역 적용 조건은 {@link SetupCompletionService} 가 따로 본다.
 */
final class SetupCompletionGate {

    private static final Set<String> READY_OR_LATER = Set.of("READY_FOR_ZONES", "BAKING_TEXTURE", "COMPLETED");

    private SetupCompletionGate() {
    }

    /**
     * 세션 쪽에서 완료를 막는 이유(없으면 null). 순서: 단계·실패 → (REUSE) World 채택 → 카메라 확인 → (NEW) 정밀 맵.
     *
     * @param status 세션 단계(FAILED 포함)
     * @param mode 실제로 따른 모드(NEW|REUSE, 모르면 NEW 로 본다)
     * @param anchorAccepted world_anchor.accepted
     * @param sceneVersion 세션 정밀 맵(setup_session.scene_version)
     * @param bakeSkipped 작업 시작으로 정식 굽기를 회수했는지(bake_skipped 값, 없으면 null)
     * @param cameraCheckPassed {@link SetupCameraCheck#passed} 결과
     */
    static ErrorCode.Setup blockedReason(String status, String mode, boolean anchorAccepted, String sceneVersion,
            String bakeSkipped, boolean cameraCheckPassed) {
        if (status == null || !READY_OR_LATER.contains(status)) {
            return ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE;
        }
        boolean reuse = SetupReuseMode.REUSE.equals(mode);
        if (reuse && !anchorAccepted) {
            return ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE;
        }
        if (!cameraCheckPassed) {
            return ErrorCode.Setup.SETUP_CAMERA_CHECK_NOT_PASSED;
        }
        if (!reuse && sceneVersion == null) {
            return bakeSkipped != null ? ErrorCode.Setup.SETUP_FINAL_MAP_SKIPPED : ErrorCode.Setup.SETUP_FINAL_MAP_NOT_READY;
        }
        return null;
    }
}
