package com.ssafy.simai.domain.scene;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.nio.charset.StandardCharsets;
import java.sql.ResultSet;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentMatchers;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.core.namedparam.SqlParameterSource;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** DB 통합 시험과 별도로 출처 지도 순서·부분 실패·레거시/기본 배경의 응답 규칙을 검증한다. */
class SceneServiceTest {

    private final NamedParameterJdbcTemplate jdbc = mock(NamedParameterJdbcTemplate.class);
    private final ObjectStorage storage = mock(ObjectStorage.class);
    private final JsonMapper mapper = JsonMapper.builder().build();
    private final ResultSet row = mock(ResultSet.class);
    private final SceneCompatibility compatibility = mock(SceneCompatibility.class);
    private final SceneService scenes = new SceneService(jdbc, storage, mapper, compatibility, "demo", "scenes/demo.glb");
    private boolean hasActive = true;

    @BeforeEach
    void setUp() throws Exception {
        doAnswer(call -> {
            if (!hasActive) {
                return List.of();
            }
            RowMapper<Object> rowMapper = call.getArgument(2);
            return List.of(rowMapper.mapRow(row, 0));
        }).when(jdbc).query(anyString(), anyMap(), ArgumentMatchers.<RowMapper<Object>>any());
        when(compatibility.current("scene-a", "cal-a")).thenReturn(mapper.createObjectNode()
                .put("scene_version", "scene-a").put("calibration_version", "cal-live")
                .put("verification", "VERIFIED").put("regeneration_required", false));
        when(row.getString("scene_version")).thenReturn("scene-a");
        when(row.getString("calibration_version")).thenReturn("cal-a");
        when(row.getString("calibration_state")).thenReturn("PROVISIONAL");
        when(row.getString("gltf_key")).thenReturn("static-3d/job/lease/scene.glb");
        when(storage.configured()).thenReturn(true);
        when(storage.uri(anyString())).thenAnswer(call -> "s3://test-bucket/" + call.getArgument(0));
        when(storage.signGet(anyString())).thenAnswer(call -> signed(call.getArgument(0)));
    }

    @Test
    void 재질_순서대로_지도를_서명하고_보정상태와_지표를_유지한다() throws Exception {
        when(row.getString("quality")).thenReturn("{\"observed_surface_ratio\":0.72}");
        maps("material-z.png", "material-a.png");
        ObjectNode result = scenes.current();
        assertThat(result.get("calibration_state").asString()).isEqualTo("PROVISIONAL");
        assertThat(result.at("/compatibility/calibration_version").asString()).isEqualTo("cal-live");
        assertThat(result.at("/compatibility/verification").asString()).isEqualTo("VERIFIED");
        assertThat(result.at("/surface_source_map_urls/0").asString()).isEqualTo("https://objects.test/static-3d/material-z.png");
        assertThat(result.at("/surface_source_map_urls/1").asString()).isEqualTo("https://objects.test/static-3d/material-a.png");
        assertThat(result.at("/quality/observed_surface_ratio").asDouble()).isEqualTo(0.72);
        assertThat(result.get("gltf_url").asString()).endsWith("/scene.glb");

        when(row.getString("calibration_state")).thenReturn(null);
        assertThat(scenes.current().has("calibration_state")).isFalse();
    }

    @Test
    void 한_지도라도_서명에_실패하면_부분_배열을_주지_않는다() throws Exception {
        maps("first.png", "second.png");
        when(storage.signGet("static-3d/second.png")).thenThrow(new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE));
        ObjectNode result = scenes.current();
        assertThat(result.has("surface_source_map_urls")).isFalse();
        assertThat(result.get("scene_version").asString()).isEqualTo("scene-a");
        assertThat(result.get("gltf_url").isString()).isTrue();
    }

    @Test
    void 지도가_없는_과거_Scene과_저장소_미설정은_지도_필드를_생략한다() throws Exception {
        assertThat(scenes.current().has("surface_source_map_urls")).isFalse();
        maps("first.png");
        when(storage.configured()).thenReturn(false);
        ObjectNode result = scenes.current();
        assertThat(result.has("surface_source_map_urls")).isFalse();
        assertThat(result.get("gltf_url").isNull()).isTrue();
        assertThat(result.get("scene_version").asString()).isEqualTo("scene-a");
    }

    @Test
    void 활성_버전이_없으면_설정_배경을_주고_설정도_없으면_404다() {
        hasActive = false;
        ObjectNode result = scenes.current();
        assertThat(result.get("default").asBoolean()).isTrue();
        assertThat(result.has("calibration_state")).isFalse();
        assertThat(result.get("gltf_url").asString()).endsWith("/scenes/demo.glb");
        assertThat(result.has("surface_source_map_urls")).isFalse();
        SceneService empty = new SceneService(jdbc, storage, mapper, mock(SceneCompatibility.class), "demo", "");
        assertThatThrownBy(empty::current).isInstanceOfSatisfying(BusinessException.class,
                e -> assertThat(e.getErrorCode()).isEqualTo(ErrorCode.Scene.SCENE_NOT_FOUND));
    }

    @Test
    void 현재_버전은_요청마다_읽는다() throws Exception {
        assertThat(scenes.current().get("scene_version").asString()).isEqualTo("scene-a");
        when(row.getString("scene_version")).thenReturn("scene-b");
        assertThat(scenes.current().get("scene_version").asString()).isEqualTo("scene-b");
    }

    @Test
    void 맵_카메라_자세가_있으면_응답에_포함하고_없으면_생략한다() throws Exception {
        assertThat(scenes.current().has("map_cameras")).isFalse();

        ObjectNode output = mapper.createObjectNode();
        ObjectNode camA = output.putObject("method").putObject("map_cameras").putObject("cam_A");
        camA.putArray("image_size").add(2304).add(1296);
        camA.set("K", mapper.readTree("[[1100.0,0.0,1152.0],[0.0,1100.0,648.0],[0.0,0.0,1.0]]"));
        camA.set("R", mapper.readTree("[[0.0,-1.0,0.0],[0.0,0.0,-1.0],[1.0,0.0,0.0]]"));
        camA.set("t", mapper.readTree("[-0.5,2.5,1.2]"));
        camA.put("pose_source", "MA_ALIGNED_SIM3");
        when(row.getString("output")).thenReturn(output.toString());

        ObjectNode result = scenes.current();
        assertThat(result.has("map_cameras")).isTrue();
        assertThat(result.at("/map_cameras/cam_A/pose_source").asString()).isEqualTo("MA_ALIGNED_SIM3");
        assertThat(result.at("/map_cameras/cam_A/image_size/0").asInt()).isEqualTo(2304);
        assertThat(result.at("/map_cameras/cam_A/t/2").asDouble()).isEqualTo(1.2);
    }

    @Test
    void 세팅_Scene은_scene_output이_없으면_그_보정_문서_카메라_자세를_CALIBRATION으로_싣는다() throws Exception {
        calibrationDocument("""
                {"calibration_version":"cal-a","cameras":[
                 {"camera_id":"cam_A","image_size":[2304,1296],"K":[[1100.0,0.0,1152.0],[0.0,1100.0,648.0],[0.0,0.0,1.0]],
                  "distortion":{"model":"NONE","coeffs":[]},"R":[[0.0,-1.0,0.0],[0.0,0.0,-1.0],[1.0,0.0,0.0]],"t":[-0.5,2.5,1.2]}]}
                """);

        ObjectNode result = scenes.current();

        assertThat(result.at("/map_cameras/cam_A/pose_source").asString()).isEqualTo("CALIBRATION");
        assertThat(result.at("/map_cameras/cam_A/image_size/1").asInt()).isEqualTo(1296);
        assertThat(result.at("/map_cameras/cam_A/K/0/0").asDouble()).isEqualTo(1100.0);
        assertThat(result.at("/map_cameras/cam_A/R/2/0").asDouble()).isEqualTo(1.0);
        assertThat(result.at("/map_cameras/cam_A/t/1").asDouble()).isEqualTo(2.5);
        assertThat(result.at("/map_cameras/cam_A").has("distortion")).isFalse();
    }

    @Test
    void 보정_문서_카메라_한_대라도_형식이_틀리면_map_cameras를_싣지_않는다() throws Exception {
        calibrationDocument("""
                {"cameras":[
                 {"camera_id":"cam_A","image_size":[2304,1296],"K":[[1100.0,0.0,1152.0],[0.0,1100.0,648.0],[0.0,0.0,1.0]],
                  "R":[[1.0,0.0,0.0],[0.0,1.0,0.0],[0.0,0.0,1.0]],"t":[0.0,0.0,0.0]},
                 {"camera_id":"cam_B","image_size":[2304.5,1296],"K":[[1100.0,0.0,1152.0],[0.0,1100.0,648.0],[0.0,0.0,1.0]],
                  "R":[[1.0,0.0,0.0],[0.0,1.0,0.0],[0.0,0.0,1.0]],"t":[0.0,0.0,0.0]}]}
                """);
        assertThat(scenes.current().has("map_cameras")).isFalse();

        calibrationDocument("not json");
        assertThat(scenes.current().has("map_cameras")).isFalse();
    }

    private void calibrationDocument(String json) {
        // 세팅 Scene 이 아니면(setup_session_artifact 없음) 질의가 빈 목록이다(모의 기본값). 여기서는 세팅 Scene 이다
        when(jdbc.queryForList(anyString(), any(SqlParameterSource.class), eq(byte[].class)))
                .thenReturn(List.of(json.getBytes(StandardCharsets.UTF_8)));
    }

    private void maps(String... names) throws Exception {
        ObjectNode output = mapper.createObjectNode();
        var assets = output.putArray("assets");
        for (String name : names) {
            assets.addObject().put("kind", "SURFACE_SOURCE_MAP").put("uri", "s3://test-bucket/static-3d/" + name);
        }
        when(row.getString("output")).thenReturn(output.toString());
    }

    private ObjectStorage.SignedRequest signed(String key) {
        return new ObjectStorage.SignedRequest("https://objects.test/" + key, Map.of(), Instant.now().plusSeconds(300));
    }
}
