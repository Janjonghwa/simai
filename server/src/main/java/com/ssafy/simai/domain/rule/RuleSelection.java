package com.ssafy.simai.domain.rule;

/**
 * 프로필에 맞는 유효 규칙 판 고르기(명세 11.4-1). 구성 묶음을 만들 때(ConfigBundleService)와 묶음이 없을 때의 규칙 조회(RuleViews)가
 * 같은 판을 고르도록 이 질의 하나를 쓴다. 규칙 ID 마다 하나: 그 프로필의 판을 먼저, 없으면 운영 판, 같으면 큰 판.
 * 파라미터 :profile, 결과는 rule_version 표의 열 그대로(별칭 r).
 */
public final class RuleSelection {

    public static final String ACTIVE_FOR_PROFILE = """
            SELECT DISTINCT ON (r.rule_id) r.* FROM rule_version r
            WHERE r.valid_to IS NULL AND (r.profile = 'OPERATION' OR r.profile = :profile)
            ORDER BY r.rule_id, (r.profile = :profile) DESC, r.rule_version DESC
            """;

    private RuleSelection() {
    }
}
