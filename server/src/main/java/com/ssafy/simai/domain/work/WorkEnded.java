package com.ssafy.simai.domain.work;

/**
 * 작업 종료(실행 하나 닫음, ENDED) 트랜잭션 안에서 보내는 스프링 이벤트. 커밋 뒤에 듣는다(S15P31A101-722): 그때 연결도 열린 GPU 세션도 없는
 * Tier 0 의 열린 LIVE 이벤트는 해소 전이가 다시 오지 않으므로 EC2 가 마감한다(OrphanEventSweeper). 연결된 Tier 0 는 종료 묶음으로 스스로 닫는다(-679).
 */
public record WorkEnded(String siteId, long workId) {
}
