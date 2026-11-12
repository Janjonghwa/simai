package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** setup_zone_draft(V34) 제약을 어기는 초안 항목은 저장 전에 걸러 나머지 초안과 바깥 트랜잭션을 지킨다(S15P31A101-632). */
class ZoneDraftProblemTest {

    private final ObjectMapper mapper = new ObjectMapper();

    private ObjectNode valid() {
        ObjectNode d = mapper.createObjectNode()
                .put("candidate_id", "cand-elev-01")
                .put("candidate_kind", "ELEVATED_WORK_SURFACE")
                .put("zone_kind", "WORK_SURFACE")
                .put("eval_type", "VOLUME_3D")
                .put("z_min_m", 0.0)
                .put("z_max_m", 2.5)
                .put("top_height_m", 0.75)
                .put("recommended_name", "작업대 상부")
                .put("recommended_severity", "HIGH")
                .put("rationale", "높이 0.75m 상판")
                .put("vlm_status", "VLM")
                .putNull("confidence");
        d.putArray("polygon_xy").addArray().add(0.0).add(0.0);
        return d;
    }

    @Test
    void 제약에_맞으면_통과한다() {
        assertThat(SetupService.draftProblem(valid())).isNull();
    }

    @Test
    void 길이_목록_수_제약을_어기면_이유를_돌려준다() {
        assertThat(SetupService.draftProblem(valid().put("rationale", "가".repeat(501)))).isNotNull();
        assertThat(SetupService.draftProblem(valid().put("recommended_name", "가".repeat(161)))).isNotNull();
        assertThat(SetupService.draftProblem(valid().put("recommended_severity", "CAUTION"))).isNotNull();
        assertThat(SetupService.draftProblem(valid().put("candidate_kind", "WALL"))).isNotNull();
        assertThat(SetupService.draftProblem(valid().put("z_max_m", 1e300))).isNotNull();
        assertThat(SetupService.draftProblem(valid().put("confidence", Double.NaN))).isNotNull();
        ObjectNode noPolygon = valid();
        noPolygon.remove("polygon_xy");
        assertThat(SetupService.draftProblem(noPolygon)).isNotNull();
        ObjectNode noName = valid();
        noName.remove("candidate_id");
        assertThat(SetupService.draftProblem(noName)).isNotNull();
        assertThat(SetupService.draftProblem(mapper.createArrayNode())).isNotNull();
    }
}
