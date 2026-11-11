package com.ssafy.simai.domain.job;

import java.time.Instant;

/**
 * 하트비트·제출의 답(계약 job_lease_status 2.1.0). ACTIVE 는 늘린 만료 시각을, REJECTED 는 이유를 싣는다.
 * REJECTED 는 HTTP 409 로 보낸다. 워커는 HTTP 상태보다 본문 status 를 따른다.
 * heartbeatIntervalMs·liveWorkActive 는 ACTIVE 에만 싣는 선택 값이다(null 이면 싣지 않음). liveWorkActive 는 site_setup 대여에서
 * 같은 현장에 진행 중 작업이 있다는 정식 굽기 회수 신호다(2.1.0, S15P31A101-597).
 */
record LeaseStatus(Status status, Instant leaseExpiresAt, Reason reason, Integer heartbeatIntervalMs, Boolean liveWorkActive) {

    LeaseStatus(Status status, Instant leaseExpiresAt, Reason reason) {
        this(status, leaseExpiresAt, reason, null, null);
    }

    /** site_setup 대여의 ACTIVE 답에 하트비트 주기와 작업 시작 신호를 싣는다. ACTIVE 가 아니면 그대로 둔다(계약: ACTIVE 에만) */
    LeaseStatus withSiteSetupSignal(int intervalMs, boolean liveWork) {
        return status == Status.ACTIVE ? new LeaseStatus(status, leaseExpiresAt, reason, intervalMs, liveWork) : this;
    }

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
