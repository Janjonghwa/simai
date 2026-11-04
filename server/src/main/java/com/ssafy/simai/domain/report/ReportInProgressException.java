package com.ssafy.simai.domain.report;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;

/** 같은 작업(일일 일지는 같은 날짜)에 이미 GENERATING 상태 보고서가 있을 때 409 와 기존 report_id 를 함께 돌려준다(.agy/tbm-interface.md B절). */
public class ReportInProgressException extends BusinessException {

    private final long reportId;

    public ReportInProgressException(long reportId) {
        this(ErrorCode.Report.REPORT_IN_PROGRESS, reportId);
    }

    /** 일일 일지처럼 다른 대상(날짜)의 생성 중 충돌은 그 오류 코드로 알린다 */
    public ReportInProgressException(ErrorCode.Report code, long reportId) {
        super(code);
        this.reportId = reportId;
    }

    public long reportId() {
        return reportId;
    }
}
