package com.ssafy.simai.domain.rule;

import java.util.Map;

/** 관제 화면과 보고서의 등급 표시 이름(-660). */
public final class SeverityNames {
    public static final Map<String, String> LABELS = Map.of(
            "CRITICAL", "위험", "HIGH", "경고", "CAUTION", "주의", "INFO", "안내");

    private SeverityNames() {
    }
}
