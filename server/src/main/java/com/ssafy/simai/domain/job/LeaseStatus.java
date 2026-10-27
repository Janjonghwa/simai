package com.ssafy.simai.domain.job;

import java.time.Instant;

/**
 * 하트비트·제출의 답(계약 job_lease_status 2.0.0). ACTIVE 는 늘린 만료 시각을, REJECTED 는 이유를 싣는다.
 * REJECTED 는 HTTP 409 로 보낸다. 워커는 HTTP 상태보다 본문 status 를 따른다.
 */
record LeaseStatus(Status status, Instant leaseExpiresAt, Reason reason) {

    enum Status { ACTIVE, ACCEPTED, DUPLICATE, REJECTED }

    /**
     * LEASE_EXPIRED: 대여가 만료·재할당됐거나 다른 대여가 작업을 끝냈다. JOB_TIMED_OUT: leased_at + timeout_ms 가 지나 작업을 끝냈다.
     * JOB_CANCELLED: EC2 가 작업을 취소했다. UNKNOWN_LEASE: 그런 대여가 없거나 다른 워커·작업의 대여다.
     */
    enum Reason { LEASE_EXPIRED, JOB_TIMED_OUT, JOB_CANCELLED, UNKNOWN_LEASE }

    static LeaseStatus active(Instant leaseExpiresAt) {
        return new LeaseStatus(Status.ACTIVE, leaseExpiresAt, null);
    }

    static LeaseStatus accepted() {
        return new LeaseStatus(Status.ACCEPTED, null, null);
    }

    static LeaseStatus duplicate() {
        return new LeaseStatus(Status.DUPLICATE, null, null);
    }

    static LeaseStatus rejected(Reason reason) {
        return new LeaseStatus(Status.REJECTED, null, reason);
    }
}
