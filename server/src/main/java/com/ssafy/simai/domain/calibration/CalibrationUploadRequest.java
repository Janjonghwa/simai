package com.ssafy.simai.domain.calibration;

import com.fasterxml.jackson.annotation.JsonProperty;
import tools.jackson.databind.JsonNode;

/** 보정 문서 올리기. document 는 contracts/providers/calibration 문서, reason 은 이 버전을 쓰는 이유(calibration_version.reason) */
public record CalibrationUploadRequest(
        @JsonProperty("reason") String reason,
        @JsonProperty("document") JsonNode document) {
}
