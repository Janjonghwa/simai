package com.ssafy.simai.domain.work;

/**
 * 작업 시작(IN_PROGRESS) 직후, 작업 시작 트랜잭션 안에서 보내는 스프링 이벤트. 듣는 쪽이 실패하면 시작도 되돌아간다.
 * 현장 세팅이 대기 중인 정식 굽기를 회수하는 데 쓴다(S15P31A101-625). work 가 setup 을 직접 부르지 않게 이벤트로 둔다
 * (setup 이 이미 work 를 쓴다).
 */
public record WorkStarted(String siteId, long workId) {
}
