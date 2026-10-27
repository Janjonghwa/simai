package com.ssafy.simai.domain.calibration;

import com.fasterxml.jackson.annotation.JsonProperty;
import tools.jackson.databind.JsonNode;

/**
 * 보정 문서 올리기. document 는 contracts/providers/calibration 문서, reason 은 이 버전을 쓰는 이유(calibration_version.reason).
 * document_text 는 같은 문서를 파일 글자 그대로 담는다(둘 중 하나만). 브라우저가 JSON.parse·stringify 를 거치면 1000.0 이 1000 이
 * 되어 content_hash 가 Tier 0 와 달라지므로, 화면은 파일 내용을 document_text 로 보낸다.
 */
public record CalibrationUploadRequest(
        @JsonProperty("reason") String reason,
        @JsonProperty("document") JsonNode document,
        @JsonProperty("document_text") String documentText) {
}
