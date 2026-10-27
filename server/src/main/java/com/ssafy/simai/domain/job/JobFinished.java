package com.ssafy.simai.domain.job;

import tools.jackson.databind.JsonNode;

/**
 * 분석 작업이 끝났다(SUCCEEDED·FAILED·TIMED_OUT·CANCELLED). 작업을 넣은 기능이 결과를 자기 표에 옮길 때 듣는다(예: BEV 평면도 → floor_plan).
 * 끝낸 트랜잭션 안에서 동기로 발행한다. 듣는 쪽이 실패하면 제출도 되돌아가 워커가 다시 보낸다.
 *
 * @param result SUCCEEDED 일 때 job_callback 의 result(계약 검사를 마친 것), 아니면 null
 * @param error 실패·시간 초과일 때 analysis_job.error(code·message), 아니면 null
 */
public record JobFinished(String jobId, String jobType, String status, JsonNode result, JsonNode error) {
}
