package com.ssafy.simai.domain.config;

import com.fasterxml.jackson.annotation.JsonProperty;

/** 구성 묶음 발행 본문. rule_profile 은 OPERATION·CLASSROOM_TEST(명세 7.4), 없으면 현장 설정(기본 교실 시험, S15P31A101-669)을 쓴다 */
public record PublishRequest(@JsonProperty("rule_profile") String ruleProfile) {
}
