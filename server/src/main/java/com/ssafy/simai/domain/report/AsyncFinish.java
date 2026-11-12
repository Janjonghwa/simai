package com.ssafy.simai.domain.report;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 비동기 생성(보고서·AI 초안·작업 마무리)의 마감 기록을 한 번 더 시도한다. 마감이 DB 순간 끊김으로 실패한 채 두면 행이
 * GENERATING 으로 남아 "진행 중 하나" 유일 색인 때문에 재시작 전까지 그 작업이 409 로 막힌다(DailyReportService.fail 과 같은 규칙).
 * 두 번 다 실패하면 기동 때 복구(INTERRUPTED·결정적 문장)에 맡긴다.
 */
final class AsyncFinish {

    private static final Logger log = LoggerFactory.getLogger(AsyncFinish.class);
    static final int ATTEMPTS = 2;

    private AsyncFinish() {
    }

    /** @return 기록했으면 true */
    static boolean withRetry(String what, long reportId, Runnable finish) {
        for (int attempt = 1; attempt <= ATTEMPTS; attempt++) {
            try {
                finish.run();
                return true;
            } catch (RuntimeException e) {
                log.warn("Could not record {} for report {} (attempt {})", what, reportId, attempt, e);
            }
        }
        return false;
    }
}
