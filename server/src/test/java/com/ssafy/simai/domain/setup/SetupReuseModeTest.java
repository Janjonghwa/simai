package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.global.exception.BusinessException;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** 세팅 모드 REST 값 ↔ site_setup 1.11.0 스냅샷 mode 검사(S15P31A101-614) */
class SetupReuseModeTest {
    private final JsonMapper mapper = JsonMapper.builder().build();

    private ObjectNode snap(String mode) {
        ObjectNode node = mapper.createObjectNode().put("status", "JOINT_BA_AND_VERIFY");
        if (mode != null) {
            node.put("mode", mode);
        }
        return node;
    }

    private ObjectNode anchor(boolean accepted, String reason) {
        return mapper.createObjectNode().put("accepted", accepted).put("reason", reason).put("reference_calibration_version", "cal-a");
    }

    @Test void REST_모드는_NEW_기본과_REUSE_만_받는다() {
        assertThat(SetupReuseMode.parseRequested(null)).isEqualTo("NEW");
        assertThat(SetupReuseMode.parseRequested("REUSE")).isEqualTo("REUSE");
        assertThatThrownBy(() -> SetupReuseMode.parseRequested("REUSE_SCENE")).isInstanceOf(BusinessException.class);
        assertThatThrownBy(() -> SetupReuseMode.parseRequested("new")).isInstanceOf(BusinessException.class);
    }

    @Test void 새_모델은_모드_없는_스냅샷만_받고_world_anchor_는_저장한다() {
        ObjectNode s = snap(null);
        s.set("world_anchor", anchor(true, "ANCHORED"));
        SetupReuseMode.Applied applied = SetupReuseMode.read(mapper, "NEW", "NEW", true, false, s);
        assertThat(applied.mode()).isEqualTo("NEW");
        assertThat(applied.worldAnchorJson()).contains("ANCHORED");
        assertThatThrownBy(() -> SetupReuseMode.read(mapper, "NEW", "NEW", true, false, snap("REUSE_SCENE")))
                .isInstanceOf(BusinessException.class);
        assertThatThrownBy(() -> SetupReuseMode.read(mapper, "NEW", "NEW", true, false, snap("SUPPLEMENT_REBAKE")))
                .isInstanceOf(BusinessException.class);
    }

    @Test void 재사용_되돌림은_거부된_world_anchor_와_같은_사유여야_하고_같은_대여에서_돌아가지_않는다() {
        ObjectNode fallback = snap("FULL_SETUP").put("reuse_fallback_reason", "SCALE_OUT_OF_RANGE");
        fallback.set("world_anchor", anchor(false, "SCALE_OUT_OF_RANGE"));
        SetupReuseMode.Applied applied = SetupReuseMode.read(mapper, "REUSE", "REUSE", true, false, fallback);
        assertThat(applied.mode()).isEqualTo("NEW");
        assertThat(applied.fallbackReason()).isEqualTo("SCALE_OUT_OF_RANGE");

        ObjectNode mismatch = fallback.deepCopy().put("reuse_fallback_reason", "DISABLED");
        assertThatThrownBy(() -> SetupReuseMode.read(mapper, "REUSE", "REUSE", true, false, mismatch)).isInstanceOf(BusinessException.class);

        ObjectNode back = snap("REUSE_SCENE");
        back.set("world_anchor", anchor(true, "ANCHORED"));
        assertThatThrownBy(() -> SetupReuseMode.read(mapper, "REUSE", "NEW", true, false, back)).isInstanceOf(BusinessException.class);
        // 재대여(다른 대여)는 처음부터 다시 따른다
        assertThat(SetupReuseMode.read(mapper, "REUSE", "NEW", false, false, back).reuse()).isTrue();
        // 채택 사유와 accepted 가 어긋난 요약은 받지 않는다
        ObjectNode bad = snap("REUSE_SCENE");
        bad.set("world_anchor", anchor(true, "DISABLED"));
        assertThatThrownBy(() -> SetupReuseMode.read(mapper, "REUSE", "REUSE", true, false, bad)).isInstanceOf(BusinessException.class);
    }

    @Test void 재사용_거부_요약은_READY_전_REUSE_SCENE_스냅샷에만_싣는다() {
        // site_setup 1.18.0(S15P31A101-689): 워커는 새 모델로 넘어가지 않고 거부 요약을 보낸 뒤 실패로 끝낸다. 세션 모드는 REUSE 그대로
        ObjectNode rejected = snap("REUSE_SCENE");
        rejected.set("world_anchor", anchor(false, "CENTER_RESIDUAL_EXCEEDED"));
        SetupReuseMode.Applied applied = SetupReuseMode.read(mapper, "REUSE", "REUSE", true, false, rejected);
        assertThat(applied.reuse()).isTrue();
        assertThat(applied.fallbackReason()).isNull();
        assertThat(applied.worldAnchorJson()).contains("CENTER_RESIDUAL_EXCEEDED");
        // READY_FOR_ZONES 이후 재사용은 채택이어야 한다
        assertThatThrownBy(() -> SetupReuseMode.read(mapper, "REUSE", "REUSE", true, true, rejected))
                .isInstanceOf(BusinessException.class);
    }
}
