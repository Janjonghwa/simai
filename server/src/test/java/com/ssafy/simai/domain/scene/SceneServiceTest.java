package com.ssafy.simai.domain.scene;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.sql.ResultSet;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentMatchers;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** DB 통합 시험과 별도로 출처 지도 순서·부분 실패·레거시/기본 배경의 응답 규칙을 검증한다. */
class SceneServiceTest {

    private final NamedParameterJdbcTemplate jdbc = mock(NamedParameterJdbcTemplate.class);
    private final ObjectStorage storage = mock(ObjectStorage.class);
    private final JsonMapper mapper = JsonMapper.builder().build();
    private final ResultSet row = mock(ResultSet.class);
    private final SceneService scenes = new SceneService(jdbc, storage, mapper, "demo", "scenes/demo.glb");
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
        when(row.getString("scene_version")).thenReturn("scene-a");
        when(row.getString("calibration_version")).thenReturn("cal-a");
        when(row.getString("gltf_key")).thenReturn("static-3d/job/lease/scene.glb");
        when(storage.configured()).thenReturn(true);
        when(storage.uri(anyString())).thenAnswer(call -> "s3://test-bucket/" + call.getArgument(0));
        when(storage.signGet(anyString())).thenAnswer(call -> signed(call.getArgument(0)));
    }

    @Test
    void 재질_순서대로_지도를_서명하고_지표를_유지한다() throws Exception {
        when(row.getString("quality")).thenReturn("{\"observed_surface_ratio\":0.72}");
        maps("material-z.png", "material-a.png");
        ObjectNode result = scenes.current();
        assertThat(result.at("/surface_source_map_urls/0").asString()).isEqualTo("https://objects.test/static-3d/material-z.png");
        assertThat(result.at("/surface_source_map_urls/1").asString()).isEqualTo("https://objects.test/static-3d/material-a.png");
        assertThat(result.at("/quality/observed_surface_ratio").asDouble()).isEqualTo(0.72);
        assertThat(result.get("gltf_url").asString()).endsWith("/scene.glb");
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
        assertThat(result.get("gltf_url").asString()).endsWith("/scenes/demo.glb");
        assertThat(result.has("surface_source_map_urls")).isFalse();
        SceneService empty = new SceneService(jdbc, storage, mapper, "demo", "");
        assertThatThrownBy(empty::current).isInstanceOfSatisfying(BusinessException.class,
                e -> assertThat(e.getErrorCode()).isEqualTo(ErrorCode.Scene.SCENE_NOT_FOUND));
    }

    @Test
    void 현재_버전은_요청마다_읽는다() throws Exception {
        assertThat(scenes.current().get("scene_version").asString()).isEqualTo("scene-a");
        when(row.getString("scene_version")).thenReturn("scene-b");
        assertThat(scenes.current().get("scene_version").asString()).isEqualTo("scene-b");
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
