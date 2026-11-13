package com.ssafy.simai.domain.calibration;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import org.assertj.core.data.Offset;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

class CalibrationViewsTest {

    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private final CalibrationViews views = new CalibrationViews(null, MAPPER);

    @Test
    void 보정_문서에서_카메라_위치_정면_단위벡터_수평시야각만_계산한다() throws Exception {
        byte[] content = Files.readAllBytes(CONTRACTS.resolve("examples/calibration.g1_auto.json"));
        ArrayNode poses = views.cameraPoses(content);

        assertThat(poses).hasSize(4);

        JsonNode cam_A = poses.get(0);
        assertThat(cam_A.get("camera_id").asString()).isEqualTo("cam_A");
        assertThat(cam_A.get("position_xyz").get(0).asDouble()).isCloseTo(0.2001, Offset.offset(1e-3));
        assertThat(cam_A.get("position_xyz").get(1).asDouble()).isCloseTo(0.2001, Offset.offset(1e-3));
        assertThat(cam_A.get("position_xyz").get(2).asDouble()).isCloseTo(2.4998, Offset.offset(1e-3));
        double fx = cam_A.get("forward_xyz").get(0).asDouble();
        double fy = cam_A.get("forward_xyz").get(1).asDouble();
        double fz = cam_A.get("forward_xyz").get(2).asDouble();
        assertThat(fx).isCloseTo(0.7574, Offset.offset(1e-3));
        assertThat(fy).isCloseTo(0.5581, Offset.offset(1e-3));
        assertThat(fz).isCloseTo(-0.3388, Offset.offset(1e-3));
        assertThat(Math.sqrt(fx * fx + fy * fy + fz * fz)).isCloseTo(1.0, Offset.offset(1e-6));
        assertThat(cam_A.get("hfov_rad").asDouble())
                .isCloseTo(2.0 * Math.atan(1920.0 / (2.0 * 1010.0)), Offset.offset(1e-6));

        assertThat(cam_A.has("R")).isFalse();
        assertThat(cam_A.has("t")).isFalse();
        assertThat(cam_A.has("K")).isFalse();
        assertThat(cam_A.has("distortion")).isFalse();

        JsonNode cam_B = poses.get(1);
        assertThat(cam_B.get("camera_id").asString()).isEqualTo("cam_B");
        assertThat(cam_B.get("position_xyz").get(0).asDouble()).isCloseTo(7.7996, Offset.offset(1e-3));
        assertThat(cam_B.get("position_xyz").get(1).asDouble()).isCloseTo(0.2004, Offset.offset(1e-3));
        assertThat(cam_B.get("position_xyz").get(2).asDouble()).isCloseTo(2.4994, Offset.offset(1e-3));
        assertThat(cam_B.get("hfov_rad").asDouble())
                .isCloseTo(2.0 * Math.atan(1920.0 / (2.0 * 1010.0)), Offset.offset(1e-6));
    }

    @Test
    void 보정_문서가_없거나_깨졌거나_비직교_회전행렬이면_빈_목록을_돌려준다() throws Exception {
        assertThat(views.cameraPoses(null)).isEmpty();
        assertThat(views.cameraPoses(new byte[0])).isEmpty();
        assertThat(views.cameraPoses("{broken-json".getBytes(StandardCharsets.UTF_8))).isEmpty();

        ObjectNode doc = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples/calibration.g1_auto.json")));
        ObjectNode badR = doc.deepCopy();
        ((ObjectNode) badR.get("cameras").get(0)).set("R",
                MAPPER.valueToTree(new int[][] {{1, 0, 0}, {0, 1, 0}, {0, 0, 0}}));
        assertThat(views.cameraPoses(MAPPER.writeValueAsBytes(badR))).isEmpty();

        ObjectNode badK = doc.deepCopy();
        ((ObjectNode) badK.get("cameras").get(0)).set("K",
                MAPPER.valueToTree(new double[][] {{0, 0, 960}, {0, 1000, 540}, {0, 0, 1}}));
        assertThat(views.cameraPoses(MAPPER.writeValueAsBytes(badK))).isEmpty();

        ObjectNode badCoord = doc.deepCopy();
        ((ObjectNode) badCoord.get("coordinate_frame")).put("up_axis", "+Y");
        assertThat(views.cameraPoses(MAPPER.writeValueAsBytes(badCoord))).isEmpty();
    }
}
