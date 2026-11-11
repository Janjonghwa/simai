package com.ssafy.simai.domain.setup;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.util.Set;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 모드 새 모델(NEW) / 이전 모델 재사용(REUSE)의 REST 값과 site_setup 1.11.0 스냅샷 값을 잇는다
 * (S15P31A101-614, docs/design/S15P31A101-614-setup-reuse-mode.md).
 * <ul>
 *   <li>REST {@code mode}: NEW(기본) | REUSE. 작업 입력 {@code mode}: NEW → 생략(FULL_SETUP), REUSE → REUSE_SCENE.
 *   <li>스냅샷 {@code mode} 는 그 대여가 실제로 따르는 모드다. REUSE_SCENE 이면 세션 mode=REUSE, FULL_SETUP(또는 생략)이면 NEW.
 *       REUSE 요청이 World 다시 맞추기 거부로 새 모델로 바뀌면 스냅샷이 {@code reuse_fallback_reason} 을 함께 싣는다.
 *   <li>같은 대여 안에서 NEW 로 바뀐 뒤 REUSE 로 돌아가지 않는다(워커는 판정 뒤 되돌리지 않는다). 재대여는 첫 스냅샷부터 다시 따른다.
 * </ul>
 * 계약 검사(스키마)는 워커 API 가 먼저 한다. 여기서는 세션의 요청 모드와 맞는지, 저장할 값을 정리한다.
 */
final class SetupReuseMode {

    static final String NEW = "NEW";
    static final String REUSE = "REUSE";
    /** site_setup 1.11.0 입력·스냅샷 mode 값 */
    static final String REUSE_SCENE = "REUSE_SCENE";
    private static final String FULL_SETUP = "FULL_SETUP";
    private static final Set<String> ANCHOR_REASONS = Set.of("ANCHORED", "DISABLED", "PREVIOUS_NOT_USABLE", "SITE_MISMATCH",
            "INSUFFICIENT_CAMERAS", "DEGENERATE_LAYOUT", "CENTER_RESIDUAL_EXCEEDED", "SCALE_OUT_OF_RANGE",
            "ORIENTATION_CHANGE_EXCEEDED", "NO_REFERENCE");

    private SetupReuseMode() {
    }

    /** REST mode → NEW | REUSE. 없으면 NEW, 그 밖의 값은 400 */
    static String parseRequested(String raw) {
        if (raw == null || NEW.equals(raw)) {
            return NEW;
        }
        if (REUSE.equals(raw)) {
            return REUSE;
        }
        throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_REQUEST);
    }

    /** 이 스냅샷이 재사용(REUSE_SCENE)을 따르는지 */
    static boolean reuseScene(JsonNode snapshot) {
        return snapshot != null && REUSE_SCENE.equals(snapshot.path("mode").asString(""));
    }

    /** 스냅샷에서 읽은 모드. worldAnchorJson 은 이 스냅샷에 없으면 null(이전 값을 둔다) */
    record Applied(String mode, String fallbackReason, String worldAnchorJson) {
        boolean reuse() {
            return REUSE.equals(mode);
        }
    }

    /**
     * 스냅샷의 mode·reuse_fallback_reason·world_anchor 를 세션 요청 모드와 맞춰 본다. 어긋나면 INVALID_SETUP_PROGRESS.
     *
     * @param currentMode 세션에 저장된 지금 모드
     * @param sameLease   이 스냅샷이 지금 진행 기준 대여와 같은 대여인지
     */
    static Applied read(ObjectMapper mapper, String requestedMode, String currentMode, boolean sameLease, JsonNode snapshot) {
        String snapshotMode = text(snapshot, "mode");
        String fallback = text(snapshot, "reuse_fallback_reason");
        JsonNode anchor = snapshot.get("world_anchor");
        boolean hasAnchor = anchor != null && !anchor.isNull();
        if (hasAnchor) {
            validateAnchor(anchor);
        }
        String mode;
        if (REUSE_SCENE.equals(snapshotMode)) {
            if (!REUSE.equals(requestedMode) || fallback != null
                    || (hasAnchor && !anchor.path("accepted").asBoolean(false))) {
                throw invalid();
            }
            mode = REUSE;
        } else if (snapshotMode == null || FULL_SETUP.equals(snapshotMode)) {
            if (fallback != null) {
                // REUSE 를 요청했고 World 다시 맞추기가 거부돼 새 모델로 이어 간다
                if (!REUSE.equals(requestedMode) || !hasAnchor || anchor.path("accepted").asBoolean(false)
                        || !fallback.equals(anchor.path("reason").asString(""))) {
                    throw invalid();
                }
            } else if (REUSE.equals(requestedMode)) {
                // REUSE 요청 작업은 판정 전 REUSE_SCENE, 판정 뒤 REUSE_SCENE 이나 FULL_SETUP + 이유만 보낸다
                throw invalid();
            }
            mode = NEW;
        } else {
            throw invalid();  // SUPPLEMENT_REBAKE 는 세션 원래 작업이 아니라 다시 굽기 작업이다(-607)
        }
        // REUSE 요청 세션은 REUSE 로 시작하므로 지금 NEW 면 이 대여가 이미 새 모델로 바뀐 것이다. 같은 대여에서 되돌아가지 않는다
        if (sameLease && REUSE.equals(mode) && NEW.equals(currentMode)) {
            throw invalid();
        }
        return new Applied(mode, fallback, hasAnchor ? mapper.writeValueAsString(anchor) : null);
    }

    private static void validateAnchor(JsonNode anchor) {
        if (!anchor.isObject() || !anchor.path("accepted").isBoolean()
                || !ANCHOR_REASONS.contains(anchor.path("reason").asString(""))) {
            throw invalid();
        }
        boolean accepted = anchor.path("accepted").asBoolean();
        if (accepted != "ANCHORED".equals(anchor.path("reason").asString())) {
            throw invalid();
        }
        JsonNode reference = anchor.get("reference_calibration_version");
        if (reference == null || !(reference.isNull() || (reference.isString() && !reference.asString().isBlank()))
                || (accepted && reference.isNull())) {
            throw invalid();
        }
    }

    /** 세션 조회 응답에 world_anchor 를 싣는다(받은 적 없으면 null) */
    static void putWorldAnchor(ObjectMapper mapper, ObjectNode out, String raw) {
        out.set("world_anchor", raw == null ? mapper.nullNode() : mapper.readTree(raw));
    }

    private static String text(JsonNode node, String field) {
        JsonNode child = node.get(field);
        if (child == null || child.isNull()) {
            return null;
        }
        if (!child.isString() || child.asString().isBlank()) {
            throw invalid();
        }
        return child.asString();
    }

    private static BusinessException invalid() {
        return new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
    }
}
