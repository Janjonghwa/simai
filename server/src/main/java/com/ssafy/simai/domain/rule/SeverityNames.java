package com.ssafy.simai.domain.rule;

import java.util.Map;
import java.util.Set;

/** 관제 화면과 보고서의 등급 표시 이름(-660). */
public final class SeverityNames {
    public static final Map<String, String> LABELS = Map.of(
            "CRITICAL", "위험", "HIGH", "경고", "CAUTION", "주의", "INFO", "안내");

    /**
     * 집계 2단(위험·주의) 중 「주의」 칸에 함께 세는 등급(S15P31A101-846). 알림 4단계 뒤 R5·R9·R11 같은 주의 경보는 CAUTION 으로
     * 저장되고, 관제 화면(front features/events/alarmTone.ts)도 HIGH 와 CAUTION 을 같은 주의 색으로 보인다. 그런데 집계가 HIGH 만 세어
     * 주의 경보가 달력에서 빠졌다. 그래서 작업 목록·달력의 event_counts·incident_counts 와 종료 미팅의 실행·사건 요약은 둘을 한 칸(응답
     * 칸 이름은 그대로 HIGH)에 센다. INFO 는 세지 않는다. 알림 발송·워치·VLM 같은 정책 판정에는 쓰지 않는다(그쪽은 등급을 따로 본다).
     */
    public static final Set<String> CAUTION_TIER = Set.of("HIGH", "CAUTION");

    /** {@link #CAUTION_TIER} 의 SQL 목록 조각: {@code e.severity IN ('HIGH', 'CAUTION')}. */
    public static final String CAUTION_TIER_SQL = "('HIGH', 'CAUTION')";

    /** 집계 2단 전체(위험 + 주의)의 SQL 목록 조각. INFO 는 빠진다. */
    public static final String COUNTED_SQL = "('CRITICAL', 'HIGH', 'CAUTION')";

    /** 집계 2단 중 주의 칸에 드는 등급인지. */
    public static boolean isCautionTier(String severity) {
        return severity != null && CAUTION_TIER.contains(severity);
    }

    private SeverityNames() {
    }
}
