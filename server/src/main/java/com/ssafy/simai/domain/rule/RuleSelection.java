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

    /** 현장 설정이 없을 때의 규칙 기준. 시연 고정으로 교실 시험(S15P31A101-669, site.rule_profile 의 DB 기본값 V67 과 같다) */
    public static final String DEFAULT_PROFILE = "CLASSROOM_TEST";

    /**
     * 현장 규칙 기준(site.rule_profile, S15P31A101-654). 적용된 묶음이 없을 때 규칙 조회·TBM 이 묶음을 만들 때와 같은 판을 고르도록 쓴다.
     * 현장 행이 없으면 DEFAULT_PROFILE. 파라미터 :site, 결과는 한 행 한 열
     */
    public static final String SITE_PROFILE = "SELECT COALESCE((SELECT rule_profile FROM site WHERE site_id = :site), '"
            + DEFAULT_PROFILE + "')";

    private RuleSelection() {
    }
}
