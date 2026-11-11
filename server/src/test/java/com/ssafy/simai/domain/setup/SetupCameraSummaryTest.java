package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.file.Files;
import java.nio.file.Path;
import org.assertj.core.data.Offset;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** 보정 문서 → 세팅 세션 카메라별 요약(S15P31A101-602) */
class SetupCameraSummaryTest {

    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final Offset<Double> EPS = Offset.offset(1e-6);

    private static ObjectNode example(String name) throws Exception {
        return (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples/" + name)));
    }

    @Test
    void 위치_정면_방위_피치_시야각과_문서_값_world_anchor_를_카메라별로_싣는다() throws Exception {
        JsonNode cameras = SetupCameraSummary.of(MAPPER, MAPPER.writeValueAsBytes(example("calibration.world_anchor.json")));

        assertThat(cameras.isArray()).isTrue();
        assertThat(cameras).hasSize(4);
        JsonNode cam1 = cameras.get(0);
        assertThat(cam1.get("camera_id").asString()).isEqualTo("cam1");
        double fx = cam1.at("/forward_xyz/0").asDouble(), fy = cam1.at("/forward_xyz/1").asDouble();
        double fz = cam1.at("/forward_xyz/2").asDouble();
        assertThat(cam1.get("yaw_rad").asDouble()).isCloseTo(Math.atan2(fy, fx), EPS);
        assertThat(cam1.get("pitch_rad").asDouble()).isCloseTo(Math.asin(fz), EPS);
        // 아래를 보는 천장 카메라는 피치가 음수
        assertThat(cam1.get("pitch_rad").asDouble()).isNegative();
        assertThat(cam1.get("hfov_rad").asDouble()).isCloseTo(2 * Math.atan(1920.0 / (2 * 1010.0)), EPS);
        assertThat(cam1.get("position_xyz")).hasSize(3);
        // 위치 Z 와 카메라 높이는 같은 World(+Z 상방) 값
        assertThat(cam1.at("/position_xyz/2").asDouble()).isCloseTo(cam1.get("camera_height_m").asDouble(), Offset.offset(1e-3));
        assertThat(cam1.get("image_size").get(0).asInt()).isEqualTo(1920);
        assertThat(cam1.get("image_size").get(1).asInt()).isEqualTo(1080);
        assertThat(cam1.get("camera_height_m").asDouble()).isEqualTo(2.5098);
        assertThat(cam1.at("/reprojection_error_px/median").asDouble()).isEqualTo(1.2);
        assertThat(cam1.at("/reprojection_error_px/p95").asDouble()).isEqualTo(3.4);

        JsonNode anchor = cameras.get(1).get("world_anchor");
        assertThat(anchor.get("reference_calibration_version").asString()).isEqualTo("cal-1-jba2-setup");
        assertThat(anchor.get("accepted").asBoolean()).isTrue();
        assertThat(anchor.get("reason").asString()).isEqualTo("ANCHORED");
        assertThat(anchor.get("orientation_change_rad").asDouble()).isEqualTo(0.009434);
        assertThat(anchor.get("center_residual_m").asDouble()).isEqualTo(0.0437);
        assertThat(anchor.get("applied_center_residual_m").asDouble()).isEqualTo(0.0369);
        assertThat(anchor.get("vertical_difference_m").asDouble()).isEqualTo(-0.0107);
        // 원본 행렬은 싣지 않는다
        assertThat(cam1.has("R")).isFalse();
        assertThat(cam1.has("K")).isFalse();
    }

    @Test
    void 문서에_없는_값은_null_로_두고_지어내지_않는다() throws Exception {
        ObjectNode doc = example("calibration.world_anchor.json");
        ObjectNode cam1 = (ObjectNode) doc.withArray("cameras").get(0);
        cam1.remove("camera_height_m");
        cam1.remove("reprojection_error_px");
        ((ObjectNode) doc.get("cameras").get(1)).putObject("reprojection_error_px").put("median", 1.4);
        ObjectNode anchor = (ObjectNode) doc.get("world_anchor");
        ((ObjectNode) anchor.get("center_residuals_m")).remove("cam2");

        JsonNode cameras = SetupCameraSummary.of(MAPPER, MAPPER.writeValueAsBytes(doc));
        assertThat(cameras.get(0).get("camera_height_m").isNull()).isTrue();
        assertThat(cameras.get(0).get("reprojection_error_px").isNull()).isTrue();
        assertThat(cameras.get(1).at("/reprojection_error_px/median").asDouble()).isEqualTo(1.4);
        assertThat(cameras.get(1).at("/reprojection_error_px/p95").isNull()).isTrue();
        assertThat(cameras.get(1).at("/world_anchor/center_residual_m").isNull()).isTrue();
        assertThat(cameras.get(1).at("/world_anchor/orientation_change_rad").asDouble()).isEqualTo(0.009434);

        // world_anchor 가 없거나 비교한 이전 보정이 없으면 카메라별 world_anchor 는 null
        doc.remove("world_anchor");
        assertThat(SetupCameraSummary.of(MAPPER, MAPPER.writeValueAsBytes(doc)).get(0).get("world_anchor").isNull()).isTrue();
        ObjectNode disabled = example("calibration.world_anchor_disabled.json");
        ((ObjectNode) disabled.get("world_anchor")).putNull("reference_calibration_version");
        assertThat(SetupCameraSummary.of(MAPPER, MAPPER.writeValueAsBytes(disabled)).get(0).get("world_anchor").isNull()).isTrue();
    }

    @Test
    void 문서가_없거나_깨졌거나_자세를_계산할_수_없으면_null_이다() throws Exception {
        assertThat(SetupCameraSummary.of(MAPPER, null).isNull()).isTrue();
        assertThat(SetupCameraSummary.of(MAPPER, new byte[0]).isNull()).isTrue();
        assertThat(SetupCameraSummary.of(MAPPER, "{not json".getBytes()).isNull()).isTrue();
        ObjectNode doc = example("calibration.world_anchor.json");
        ((ArrayNode) doc.at("/cameras/0/R/0")).set(0, MAPPER.getNodeFactory().numberNode(2.0));
        assertThat(SetupCameraSummary.of(MAPPER, MAPPER.writeValueAsBytes(doc)).isNull()).isTrue();
    }

    @Test
    void 정면이_수직이면_방위는_null_이고_피치는_마이너스_90도다() throws Exception {
        ObjectNode doc = example("calibration.world_anchor.json");
        ObjectNode cam1 = (ObjectNode) doc.withArray("cameras").get(0);
        // 똑바로 아래를 보는 카메라: R 셋째 행 = (0, 0, -1)
        cam1.set("R", MAPPER.valueToTree(new double[][] {{1, 0, 0}, {0, -1, 0}, {0, 0, -1}}));
        cam1.set("t", MAPPER.valueToTree(new double[] {0, 0, 2.5}));
        JsonNode first = SetupCameraSummary.of(MAPPER, MAPPER.writeValueAsBytes(doc)).get(0);
        assertThat(first.get("yaw_rad").isNull()).isTrue();
        assertThat(first.get("pitch_rad").asDouble()).isCloseTo(-Math.PI / 2, EPS);
        assertThat(first.at("/position_xyz/2").asDouble()).isCloseTo(2.5, EPS);
    }
}
