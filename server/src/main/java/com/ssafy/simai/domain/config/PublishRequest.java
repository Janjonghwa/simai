package com.ssafy.simai.domain.config;

import com.fasterxml.jackson.annotation.JsonProperty;

/** 구성 묶음 발행 본문. rule_profile 은 OPERATION·CLASSROOM_TEST(명세 7.4), 없으면 이전 묶음의 것을 쓴다 */
public record PublishRequest(@JsonProperty("rule_profile") String ruleProfile) {
}
