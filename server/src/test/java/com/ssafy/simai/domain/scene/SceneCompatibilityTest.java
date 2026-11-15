package com.ssafy.simai.domain.scene;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

class SceneCompatibilityTest {
    private final JsonMapper mapper = JsonMapper.builder().build();
    private final NamedParameterJdbcTemplate jdbc = mock(NamedParameterJdbcTemplate.class);
    private final SceneCompatibility service = new SceneCompatibility(jdbc, mapper, null);

    private ObjectNode metadata(String world, double x, double degrees) {
        var m = mapper.createObjectNode().put("world_frame_id", world);
        var p = m.putArray("camera_poses").addObject().put("camera_id", "cam_A");
        double a = Math.toRadians(degrees), c = Math.cos(a), s = Math.sin(a);
        p.set("R", mapper.valueToTree(new double[][] {{c,-s,0},{s,c,0},{0,0,1}}));
        // C=(x,0,2), t=-R*C. 회전과 중심 변화가 섞여도 중심으로 비교해야 한다.
        p.set("t", mapper.valueToTree(new double[] {-c*x,-s*x,-2}));
        return m;
    }

    private void versions(ObjectNode a, ObjectNode b) {
        when(jdbc.queryForList(contains("SELECT site_id"), anyMap(), eq(String.class))).thenReturn(List.of("site", "site"));
        when(jdbc.queryForList(contains("SELECT payload"), eq(Map.of("v", "source")), eq(String.class))).thenReturn(List.of(a.toString()));
        when(jdbc.queryForList(contains("SELECT payload"), eq(Map.of("v", "target")), eq(String.class))).thenReturn(List.of(b.toString()));
    }

    @Test void 같은_불변_보정과_미상_보정을_구분한다() {
        assertThat(service.compare("scene", "source", "source").path("verification").asString()).isEqualTo("VERIFIED");
        assertThat(service.compare("scene", "source", null).path("verification").asString()).isEqualTo("DIMMED");
        assertThat(service.compare("scene", "source", "missing").path("reason").asString()).isEqualTo("UNKNOWN");
    }

    @Test void 내용이_같은_보정은_버전이_달라도_같은_보정이다() {
        // S15P31A101-714: 이전 맵 재사용이 그 맵의 보정을 계보 키만 바꾼 새 버전으로 다시 낸 경우
        when(jdbc.queryForList(contains("SELECT site_id"), anyMap(), eq(String.class))).thenReturn(List.of("site", "site"));
        when(jdbc.queryForList(contains("SELECT v.calibration_hash"), anyMap(), eq(String.class)))
                .thenReturn(List.of("sha256:same", "sha256:same"));
        var out = service.compare("scene", "source", "source-reuse-1");
        assertThat(out.path("verification").asString()).isEqualTo("VERIFIED");
        assertThat(out.path("reason").asString()).isEqualTo("SAME_CALIBRATION");
    }

    @Test void 중심과_회전_경계를_포함하고_누적변화가_넘으면_재생성을_요청한다() {
        versions(metadata("root", 0, 0), metadata("root", .10, 2));
        var small = service.compare("scene", "source", "target");
        assertThat(small.path("verification").asString()).isEqualTo("VERIFIED");
        assertThat(small.path("max_center_delta_m").asDouble()).isCloseTo(.10, within(1e-9));
        assertThat(small.path("max_rotation_delta_rad").asDouble()).isCloseTo(Math.toRadians(2), within(1e-9));
        versions(metadata("root", 0, 0), metadata("root", .1001, 0));
        var large = service.compare("scene", "source", "target");
        assertThat(large.path("verification").asString()).isEqualTo("DIMMED");
        assertThat(large.path("regeneration_required").asBoolean()).isTrue();
        versions(metadata("root", 0, 0), metadata("root", 0, 2.001));
        assertThat(service.compare("scene", "source", "target").path("reason").asString()).isEqualTo("LARGE_CHANGE");
    }

    @Test void 다른_World는_작은변화라도_숨긴다() {
        versions(metadata("world-a", 0, 0), metadata("world-b", 0, 0));
        assertThat(service.compare("scene", "source", "target").path("verification").asString()).isEqualTo("MISMATCH");
    }

    @Test void 누락_중복_비직교_자세는_검증됨으로_승격하지_않는다() {
        var a = metadata("root", 0, 0);
        var b = metadata("root", 0, 0);
        b.withArray("camera_poses").add(b.path("camera_poses").get(0).deepCopy());
        versions(a, b);
        assertThat(service.compare("scene", "source", "target").path("reason").asString()).isEqualTo("UNKNOWN");
        b = metadata("root", 0, 0);
        ((tools.jackson.databind.node.ArrayNode)b.at("/camera_poses/0/R/0")).set(0, mapper.getNodeFactory().numberNode(2));
        versions(a, b);
        assertThat(service.compare("scene", "source", "target").path("verification").asString()).isEqualTo("DIMMED");
        versions(a, mapper.createObjectNode().put("world_frame_id", "root"));
        assertThat(service.compare("scene", "source", "target").path("max_center_delta_m").isNull()).isTrue();
    }

    @Test void 과거_문서는_bootstrap과_같을_때만_계보를_연결하고_다르면_미상이다() {
        var a = metadata("source", 0, 0);
        var b = metadata("source", .02, 0);
        versions(a, b);
        when(jdbc.queryForList(contains("SELECT payload"), eq(Map.of("v", "source")), eq(String.class))).thenReturn(List.of());
        var doc = mapper.createObjectNode();
        doc.set("cameras", a.path("camera_poses"));
        when(jdbc.queryForList(contains("SELECT content"), eq(Map.of("v", "source")), eq(byte[].class)))
                .thenReturn(List.of(mapper.writeValueAsBytes(doc)));
        assertThat(service.compare("scene", "source", "target").path("verification").asString()).isEqualTo("VERIFIED");
        b.put("world_frame_id", "unrelated-root");
        versions(a, b);
        when(jdbc.queryForList(contains("SELECT payload"), eq(Map.of("v", "source")), eq(String.class))).thenReturn(List.of());
        assertThat(service.compare("scene", "source", "target").path("reason").asString()).isEqualTo("UNKNOWN");
    }

    /** 카메라 둘: cam_A 는 (x, 0, 2) 에서 yaw 회전, cam_B 는 고정 */
    private ObjectNode twoCameras(String world, double x, double degrees) {
        var m = metadata(world, x, degrees);
        var p = m.withArray("camera_poses").addObject().put("camera_id", "cam_B");
        p.set("R", mapper.valueToTree(new double[][] {{1,0,0},{0,1,0},{0,0,1}}));
        p.set("t", mapper.valueToTree(new double[] {-1,-1,-2}));
        return m;
    }

    @Test void 현재_Scene_판정은_카메라별_중심_이동과_회전을_최대값_옆에_싣는다() {
        versions(twoCameras("root", 0, 0), twoCameras("root", .05, 1));
        var out = service.compareWithCameras("scene", "source", "target");
        assertThat(out.path("reason").asString()).isEqualTo("SMALL_CHANGE");
        var cameras = out.path("cameras");
        assertThat(cameras).hasSize(2);
        assertThat(cameras.get(0).path("camera_id").asString()).isEqualTo("cam_A");
        assertThat(cameras.get(0).path("center_delta_m").asDouble()).isCloseTo(.05, within(1e-9));
        assertThat(cameras.get(0).path("rotation_delta_rad").asDouble()).isCloseTo(Math.toRadians(1), within(1e-9));
        assertThat(cameras.get(1).path("camera_id").asString()).isEqualTo("cam_B");
        assertThat(cameras.get(1).path("center_delta_m").asDouble()).isCloseTo(0, within(1e-12));
        assertThat(cameras.get(1).path("rotation_delta_rad").asDouble()).isCloseTo(0, within(1e-6));
        assertThat(out.path("max_center_delta_m").asDouble()).isEqualTo(cameras.get(0).path("center_delta_m").asDouble());
        // viewer_health 용 compare 는 카메라별 값을 싣지 않는다(주기 송신 모양 유지)
        assertThat(service.compare("scene", "source", "target").has("cameras")).isFalse();
    }

    @Test void 자세를_비교하지_못하면_카메라별_값은_null_이다() {
        assertThat(service.compareWithCameras("scene", "source", "source").path("cameras").isNull()).isTrue();
        assertThat(service.compareWithCameras("scene", "source", null).path("cameras").isNull()).isTrue();
        versions(twoCameras("world-a", 0, 0), twoCameras("world-b", 0, 0));
        var different = service.compareWithCameras("scene", "source", "target");
        assertThat(different.path("reason").asString()).isEqualTo("DIFFERENT_WORLD");
        assertThat(different.path("cameras").isNull()).isTrue();
        versions(twoCameras("root", 0, 0), metadata("root", 0, 0));
        var missing = service.compareWithCameras("scene", "source", "target");
        assertThat(missing.path("reason").asString()).isEqualTo("UNKNOWN");
        assertThat(missing.has("cameras")).isTrue();
        assertThat(missing.path("cameras").isNull()).isTrue();
    }

    /** 보정 문서에 world_anchor 를 싣는다(-584). accepted 면 reference 의 World 로 옮겨진 보정이다 */
    private void anchorDoc(String version, boolean accepted, String reference) {
        var doc = mapper.createObjectNode();
        doc.putObject("world_anchor").put("accepted", accepted).put("reason", accepted ? "ANCHORED" : "CENTER_RESIDUAL_EXCEEDED")
                .put("reference_calibration_version", reference);
        when(jdbc.queryForList(contains("SELECT content"), eq(Map.of("v", version)), eq(byte[].class)))
                .thenReturn(List.of(mapper.writeValueAsBytes(doc)));
    }

    @Test void 같은_World_로_옮긴_세팅_보정은_계보_ID_가_달라도_채택_한도로_판정한다() {
        // 재사용 세팅(-614): Tier 0 는 세팅 보정을 새 계보(world-new)로 채택하지만 world_anchor 가 Scene 보정(source)의 World 로 옮겼다
        versions(twoCameras("world-old", 0, 0), twoCameras("world-new", .12, 5));
        anchorDoc("target", true, "source");
        var out = service.compareWithCameras("scene", "source", "target");
        assertThat(out.path("verification").asString()).isEqualTo("VERIFIED");
        assertThat(out.path("reason").asString()).isEqualTo("SMALL_CHANGE");
        assertThat(out.path("max_center_delta_m").asDouble()).isCloseTo(.12, within(1e-9));
        assertThat(out.path("cameras")).hasSize(2);
        // 채택 한도(0.25 m·10°)를 넘으면 같은 World 라도 흐림
        versions(twoCameras("world-old", 0, 0), twoCameras("world-new", 0, 10.5));
        anchorDoc("target", true, "source");
        assertThat(service.compareWithCameras("scene", "source", "target").path("reason").asString()).isEqualTo("LARGE_CHANGE");
    }

    @Test void 기준_보정이_Scene_과_같은_World_계보면_사슬을_따라_같은_World_로_본다() {
        // Scene 보정 source(world-old) → Tier 0 가 회전 복구로 같은 계보 후보 mid 를 씀 → 재사용 세팅 target 이 mid 에 맞춰짐
        versions(twoCameras("world-old", 0, 0), twoCameras("world-new", .05, 1));
        anchorDoc("target", true, "mid");
        when(jdbc.queryForList(contains("SELECT payload"), eq(Map.of("v", "mid")), eq(String.class)))
                .thenReturn(List.of(twoCameras("world-old", .01, 0).toString()));
        assertThat(service.compareWithCameras("scene", "source", "target").path("verification").asString()).isEqualTo("VERIFIED");
    }

    @Test void 거부했거나_다른_World_에_맞춘_세팅_보정은_여전히_다른_World_다() {
        versions(twoCameras("world-old", 0, 0), twoCameras("world-new", 0, 0));
        anchorDoc("target", false, "source");
        assertThat(service.compareWithCameras("scene", "source", "target").path("reason").asString()).isEqualTo("DIFFERENT_WORLD");
        anchorDoc("target", true, "elsewhere");
        when(jdbc.queryForList(contains("SELECT payload"), eq(Map.of("v", "elsewhere")), eq(String.class)))
                .thenReturn(List.of(twoCameras("world-other", 0, 0).toString()));
        assertThat(service.compareWithCameras("scene", "source", "target").path("verification").asString()).isEqualTo("MISMATCH");
    }

    private static org.assertj.core.data.Offset<Double> within(double n) { return org.assertj.core.data.Offset.offset(n); }
}
