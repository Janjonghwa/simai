package com.ssafy.simai.domain.scene;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.ssafy.simai.global.contract.ContractSchemas;
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
import org.springframework.jdbc.core.namedparam.SqlParameterSource;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** Scene 입력 사진 서명 주소(S15P31A101-592): 묶음 연결·키 제한·모르면 비움·계약(scene_camera_images). */
class SceneCameraImageServiceTest {

    private static final String BUNDLE = "bundle-3f6c2b1a9d8e4f70a1b2c3d4e5f60718";
    private static final String PREFIX = "s3://test-bucket/static-inputs/" + BUNDLE + "/";

    private final NamedParameterJdbcTemplate jdbc = mock(NamedParameterJdbcTemplate.class);
    private final ObjectStorage storage = mock(ObjectStorage.class);
    private final JsonMapper mapper = JsonMapper.builder().build();
    private final ResultSet row = mock(ResultSet.class);
    private final ContractSchemas schemas = new ContractSchemas();
    private final SceneCameraImageService service = new SceneCameraImageService(jdbc, storage, mapper);
    private boolean exists = true;
    private final Instant now = Instant.parse("2026-11-10T00:00:00Z");

    @BeforeEach
    void setUp() throws Exception {
        doAnswer(call -> {
            if (!exists) {
                return List.of();
            }
            RowMapper<Object> rowMapper = call.getArgument(2);
            return List.of(rowMapper.mapRow(row, 0));
        }).when(jdbc).query(anyString(), any(SqlParameterSource.class), ArgumentMatchers.<RowMapper<Object>>any());
        when(row.getString("scene_version")).thenReturn("scene-a");
        when(row.getString("bundle_id")).thenReturn(BUNDLE);
        when(storage.configured()).thenReturn(true);
        when(storage.uri(anyString())).thenAnswer(call -> "s3://test-bucket/" + call.getArgument(0));
        when(storage.signGet(anyString())).thenAnswer(call -> signed(call.getArgument(0)));
    }

    @Test
    void 작업_입력의_사람_지운_배경_사진을_카메라_순으로_서명하고_가장_이른_만료를_준다() throws Exception {
        images(image("cam_B", PREFIX + "cam_B.jpg", "CLEAN_BACKGROUND"), image("cam_A", PREFIX + "cam_A.png", "CLEAN_BACKGROUND"));
        when(storage.signGet("static-inputs/" + BUNDLE + "/cam_A.png"))
                .thenReturn(new ObjectStorage.SignedRequest("https://objects.test/a", Map.of(), now.plusSeconds(200)));

        ObjectNode body = service.images("scene-a");

        assertThat(body.path("scene_version").asString()).isEqualTo("scene-a");
        assertThat(body.path("bundle_id").asString()).isEqualTo(BUNDLE);
        assertThat(body.path("images")).hasSize(2);
        assertThat(body.path("images").get(0).path("camera_id").asString()).isEqualTo("cam_A");
        assertThat(body.path("images").get(0).path("url").asString()).isEqualTo("https://objects.test/a");
        assertThat(body.path("images").get(1).path("url").asString())
                .isEqualTo("https://objects.test/static-inputs/" + BUNDLE + "/cam_B.jpg");
        assertThat(body.path("expires_at_ms").asLong()).isEqualTo(now.plusSeconds(200).toEpochMilli());
        assertThat(schemas.violations("messages/scene_camera_images", body.toString())).isEmpty();
    }

    @Test
    void 묶음_폴더_밖_다른_종류_중복_카메라_형식이_틀린_항목은_서명하지_않는다() throws Exception {
        images(image("cam_A", PREFIX + "cam_A.jpg", "CLEAN_BACKGROUND"),
                image("cam_A", PREFIX + "cam_A_second.jpg", "CLEAN_BACKGROUND"),
                image("cam_B", "s3://test-bucket/static-inputs/bundle-00000000000000000000000000000000/cam_B.jpg", "CLEAN_BACKGROUND"),
                image("cam_C", PREFIX + "cam_C.jpg", "FRAME"),
                image("cam_D", PREFIX + "../cam_D.jpg", "CLEAN_BACKGROUND"),
                image("cam_E", PREFIX + "calibration.json", "CLEAN_BACKGROUND"),
                image("cam F", PREFIX + "cam_F.jpg", "CLEAN_BACKGROUND"),
                image("cam_G", "s3://other-bucket/static-inputs/" + BUNDLE + "/cam_G.jpg", "CLEAN_BACKGROUND"));

        ObjectNode body = service.images("scene-a");

        assertThat(body.path("images")).hasSize(1);
        assertThat(body.path("images").get(0).path("camera_id").asString()).isEqualTo("cam_A");
        verify(storage).signGet("static-inputs/" + BUNDLE + "/cam_A.jpg");
        verify(storage, never()).signGet("static-inputs/" + BUNDLE + "/cam_A_second.jpg");
        assertThat(schemas.violations("messages/scene_camera_images", body.toString())).isEmpty();
    }

    @Test
    void 작업이_없는_Scene은_묶음을_모르므로_빈_목록이다() throws Exception {
        when(row.getString("bundle_id")).thenReturn(null);
        when(row.getString("images")).thenReturn(null);

        ObjectNode body = service.images("scene-a");

        assertThat(body.path("bundle_id").isNull()).isTrue();
        assertThat(body.path("expires_at_ms").isNull()).isTrue();
        assertThat(body.path("images")).isEmpty();
        verify(storage, never()).signGet(anyString());
        assertThat(schemas.violations("messages/scene_camera_images", body.toString())).isEmpty();
    }

    @Test
    void 저장소_미설정이나_서명_실패면_묶음만_주고_목록은_비운다() throws Exception {
        images(image("cam_A", PREFIX + "cam_A.jpg", "CLEAN_BACKGROUND"), image("cam_B", PREFIX + "cam_B.jpg", "CLEAN_BACKGROUND"));
        when(storage.signGet("static-inputs/" + BUNDLE + "/cam_B.jpg"))
                .thenThrow(new BusinessException(ErrorCode.Storage.STORAGE_UNAVAILABLE));

        ObjectNode failed = service.images("scene-a");
        assertThat(failed.path("bundle_id").asString()).isEqualTo(BUNDLE);
        assertThat(failed.path("images")).isEmpty();
        assertThat(failed.path("expires_at_ms").isNull()).isTrue();
        assertThat(schemas.violations("messages/scene_camera_images", failed.toString())).isEmpty();

        when(storage.configured()).thenReturn(false);
        ObjectNode unconfigured = service.images("scene-a");
        assertThat(unconfigured.path("images")).isEmpty();
        assertThat(schemas.violations("messages/scene_camera_images", unconfigured.toString())).isEmpty();
    }

    @Test
    void 없는_Scene은_404다() {
        exists = false;
        assertThatThrownBy(() -> service.images("missing")).isInstanceOfSatisfying(BusinessException.class,
                e -> assertThat(e.getErrorCode()).isEqualTo(ErrorCode.Scene.SCENE_VERSION_NOT_FOUND));
    }

    private void images(String... items) throws Exception {
        when(row.getString("images")).thenReturn("[" + String.join(",", items) + "]");
    }

    private String image(String camera, String uri, String kind) {
        return mapper.createObjectNode().put("camera_id", camera).put("uri", uri).put("kind", kind).toString();
    }

    private ObjectStorage.SignedRequest signed(String key) {
        return new ObjectStorage.SignedRequest("https://objects.test/" + key, Map.of(), now.plusSeconds(300));
    }
}
