package com.ssafy.simai.domain.report;

import com.fasterxml.jackson.annotation.JsonProperty;

/** 일일 일지 생성 요청(POST /api/reports/daily, S15P31A101-33). report_date 를 비우면 오늘(Asia/Seoul)이다. */
public record DailyReportRequest(@JsonProperty("report_date") String reportDate) {
}
