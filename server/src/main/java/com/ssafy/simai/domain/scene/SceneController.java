package com.ssafy.simai.domain.scene;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현재 Scene 조회(명세 12.6 GET /api/scenes/current, 열람). 응답은 SceneService 설명.
 * Scene 입력 카메라 사진 서명 주소(GET /api/scenes/{sceneVersion}/camera-images, 열람, S15P31A101-592). 응답은 SceneCameraImageService 설명
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SceneController {

    private final SceneService scenes;
    private final SceneCameraImageService cameraImages;

    SceneController(SceneService scenes, SceneCameraImageService cameraImages) {
        this.scenes = scenes;
        this.cameraImages = cameraImages;
    }

    @GetMapping("/api/scenes/current")
    ObjectNode current() {
        return scenes.current();
    }

    @GetMapping("/api/scenes/{sceneVersion}/camera-images")
    ObjectNode cameraImages(@PathVariable String sceneVersion) {
        return cameraImages.images(sceneVersion);
    }
}
