package com.ssafy.simai.domain.report;

import com.fasterxml.jackson.annotation.JsonProperty;

/** TBM 보고서 생성 요청(POST /api/reports/tbm, .agy/tbm-interface.md B절). */
public record TbmReportRequest(@JsonProperty("work_id") Long workId) {
}
