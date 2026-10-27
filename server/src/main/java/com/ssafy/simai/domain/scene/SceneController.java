package com.ssafy.simai.domain.scene;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/** 현재 Scene 조회(명세 12.6 GET /api/scenes/current, 열람). 응답은 SceneService 설명 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SceneController {

    private final SceneService scenes;

    SceneController(SceneService scenes) {
        this.scenes = scenes;
    }

    @GetMapping("/api/scenes/current")
    ObjectNode current() {
        return scenes.current();
    }
}
