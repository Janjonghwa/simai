package com.ssafy.simai.domain.job;

/**
 * I5 작업이 대여(LEASED)되거나 반납·재시도로 다시 대기(QUEUED)로 바뀔 때 같은 트랜잭션에서 보내는 스프링 이벤트.
 */
public record JobStateChanged(String jobId, String jobType, String status) {
}
