package com.ssafy.simai.domain.scene;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * Scene 을 구운 입력 카메라 사진의 GET 서명 주소(GET /api/scenes/{sceneVersion}/camera-images, S15P31A101-592, 계약 scene_camera_images).
 * 관제 3D 뷰어가 카메라 시점 VDT(-590)를 그 Scene 의 map_cameras 자세에 투영하는 데만 쓴다(Visual 층, 규칙 1: 위치·판정에 들어가지 않음).
 * <ul>
 * <li>Scene ↔ 입력 묶음은 이미 DB 에 있다: scene_version.job_id(static_reconstruct, SceneJobListener) → analysis_job.input 의
 * clean_background_bundle_id·images(StaticReconstructSubmitService 가 저장). 새 열·표를 만들지 않는다.</li>
 * <li>관리자 등록·세팅 세션 Scene 처럼 작업이 없거나 묶음을 알 수 없으면 bundle_id null·빈 목록이다. 최신 묶음을 갖다 붙이지 않는다(규칙 2).</li>
 * <li>이 묶음의 static-inputs/&lt;bundle_id&gt;/ 아래 사람 지운 배경(CLEAN_BACKGROUND) 사진만 서명한다. 다른 키는 서명하지 않는다.</li>
 * <li>저장소를 쓸 수 없거나 서명에 실패하면 묶음 ID 만 주고 목록은 비운다(화면은 시점 이동만 한다). 서명 주소는 로그에 남기지 않는다.</li>
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SceneCameraImageService {

    private static final Logger log = LoggerFactory.getLogger(SceneCameraImageService.class);

    private static final Pattern BUNDLE = Pattern.compile("^bundle-[0-9a-f]{32}$");
    private static final Pattern CAMERA = Pattern.compile("^[A-Za-z0-9_-]+$");
    /** 묶음 폴더 바로 아래 사진 한 장(.jpg·.png, UploadPurpose.STATIC_RECONSTRUCT_INPUT) */
    private static final Pattern FILE = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\\.(jpg|png)$");
    /** 계약 scene_camera_images.images maxItems */
    private static final int MAX_IMAGES = 16;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectStorage storage;
    private final ObjectMapper mapper;

    public SceneCameraImageService(NamedParameterJdbcTemplate jdbc, ObjectStorage storage, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.storage = storage;
        this.mapper = mapper;
    }

    /** @throws BusinessException SCENE_VERSION_NOT_FOUND(없는 Scene) */
    public ObjectNode images(String sceneVersion) {
        List<Input> found = jdbc.query("""
                SELECT s.scene_version, j.input->>'clean_background_bundle_id' AS bundle_id, j.input->'images' AS images
                FROM scene_version s
                LEFT JOIN analysis_job j ON j.job_id = s.job_id AND j.job_type = 'static_reconstruct'
                WHERE s.scene_version = :v
                """, new MapSqlParameterSource("v", sceneVersion),
                (rs, i) -> new Input(rs.getString("scene_version"), rs.getString("bundle_id"), rs.getString("images")));
        if (found.isEmpty()) {
            throw new BusinessException(ErrorCode.Scene.SCENE_VERSION_NOT_FOUND);
        }
        Input input = found.getFirst();
        String bundle = input.bundleId() != null && BUNDLE.matcher(input.bundleId()).matches() ? input.bundleId() : null;
        ObjectNode node = mapper.createObjectNode();
        node.put("scene_version", input.sceneVersion());
        node.put("bundle_id", bundle);
        node.putNull("expires_at_ms");
        ArrayNode images = node.putArray("images");
        if (bundle == null || input.images() == null || !storage.configured()) {
            return node;
        }
        Map<String, String> keys = keys(bundle, mapper.readTree(input.images()));
        Instant expires = null;
        ArrayNode signed = mapper.createArrayNode();
        try {
            for (Map.Entry<String, String> entry : keys.entrySet()) {
                SignedRequest request = storage.signGet(entry.getValue());
                signed.addObject().put("camera_id", entry.getKey()).put("url", request.url());
                if (expires == null || request.expiresAt().isBefore(expires)) {
                    expires = request.expiresAt();
                }
            }
        } catch (BusinessException e) {
            log.warn("Scene {} 입력 사진 서명 주소를 만들지 못해 빈 목록을 준다: {}", input.sceneVersion(), e.getErrorCode());
            return node;
        }
        if (!signed.isEmpty()) {
            images.addAll(signed);
            node.put("expires_at_ms", expires.toEpochMilli());
        }
        return node;
    }

    /** 카메라 ID → 저장소 키(카메라 ID 순, 카메라당 처음 것). 이 묶음 폴더 밖이거나 형식이 틀린 항목은 버린다 */
    private Map<String, String> keys(String bundle, JsonNode images) {
        Map<String, String> keys = new TreeMap<>();
        if (!images.isArray()) {
            return keys;
        }
        String prefix = storage.uri("static-inputs/" + bundle + "/");
        String bucketPrefix = storage.uri("");
        for (JsonNode image : images) {
            String camera = image.path("camera_id").asString("");
            String uri = image.path("uri").asString("");
            if (!"CLEAN_BACKGROUND".equals(image.path("kind").asString("")) || !CAMERA.matcher(camera).matches()
                    || keys.containsKey(camera) || !uri.startsWith(prefix)
                    || !FILE.matcher(uri.substring(prefix.length())).matches()) {
                continue;
            }
            if (keys.size() >= MAX_IMAGES) {
                break;
            }
            keys.put(camera, uri.substring(bucketPrefix.length()));
        }
        return keys;
    }

    private record Input(String sceneVersion, String bundleId, String images) {
    }
}
