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

    private static org.assertj.core.data.Offset<Double> within(double n) { return org.assertj.core.data.Offset.offset(n); }
}
