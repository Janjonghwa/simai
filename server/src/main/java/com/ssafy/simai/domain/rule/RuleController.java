package com.ssafy.simai.domain.rule;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/** 규칙 버전 조회(명세 12.6 GET /api/rules, 열람). 응답은 RuleViews 설명 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class RuleController {

    private final RuleViews rules;

    RuleController(RuleViews rules) {
        this.rules = rules;
    }

    @GetMapping("/api/rules")
    ObjectNode rules() {
        return rules.rules();
    }
}
