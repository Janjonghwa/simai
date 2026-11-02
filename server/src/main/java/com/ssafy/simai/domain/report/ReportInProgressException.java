package com.ssafy.simai.domain.report;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;

/** 같은 작업에 이미 GENERATING 상태 보고서가 있을 때 409 와 기존 report_id 를 함께 돌려준다(.agy/tbm-interface.md B절). */
public class ReportInProgressException extends BusinessException {

    private final long reportId;

    public ReportInProgressException(long reportId) {
        super(ErrorCode.Report.REPORT_IN_PROGRESS);
        this.reportId = reportId;
    }

    public long reportId() {
        return reportId;
    }
}
