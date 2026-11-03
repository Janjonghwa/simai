package com.ssafy.simai.domain.job;

import java.util.UUID;
import tools.jackson.databind.JsonNode;

/** 계약 검사를 통과하고 유효 대여를 확인한 site_setup 하트비트. 대여 갱신과 같은 트랜잭션에서 전달한다. */
public record JobProgress(String jobId, UUID leaseId, JsonNode setup) { }
