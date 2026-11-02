package com.ssafy.simai.domain.work;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * TBM 작업 등록·수정 본문(V21 work_unit). 시각은 epoch ms, 날짜는 현장 시각의 YYYY-MM-DD 다(화면이 현장 시각을 안다).
 * zone_id 가 없으면 구역 밖 작업이고 place 에 장소를 적는다. lead 는 조 이름이다(사람 이름을 넣지 않는다, NFR-14).
 * 위험성 평가·대책 문장은 담지 않는다(tbm_report 가 AI 로 쓴다).
 */
public record WorkUnitRequest(
        @JsonProperty("date") String date,
        @JsonProperty("name") String name,
        @JsonProperty("from_ms") Long fromMs,
        @JsonProperty("to_ms") Long toMs,
        @JsonProperty("zone_id") String zoneId,
        @JsonProperty("place") String place,
        @JsonProperty("planned_people") Integer plannedPeople,
        @JsonProperty("lead") String lead,
        @JsonProperty("layout_id") Long layoutId,
        @JsonProperty("work_type") String workType) {
}
