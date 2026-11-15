package com.ssafy.simai.domain.work;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * 관제 화면 카드의 「사건」 수(S15P31A101-731 화면 묶음, 관제 앱 features/events/incidents.ts chainItems)를 서버에서 같은 규칙으로 센다
 * (S15P31A101-751: 달력·종료 미팅의 경보 수가 카드 「n회」 기준과 달라 보이던 것을 사건 수로 함께 보인다). 이벤트는 바꾸지 않고 숫자만 센다.
 *
 * <p>같은 위험(규칙·구역)끼리 시작 시각순으로 잇는다: 같은 사람이 이미 든 묶음이거나, 앞 묶음의 마지막 갱신에서 {@link #MERGE_GAP_MS} 안에
 * 시작했으면 앞 묶음에 넣는다. 지난 작업이라 지금 살아 있는 사람은 없으므로(화면의 alive 가 빈 집합) 서로 다른 사람도 그 시간 안이면 한 사건이다.
 * 사건의 등급은 든 경보 중 가장 높은 등급이다.
 */
public final class IncidentCounter {

    /** 관제 앱 constants.ts CARD_MERGE_GAP_MS 와 같은 값(60초). */
    static final long MERGE_GAP_MS = 60_000L;

    /** 경보 하나: 사람이 없으면 person 이 null 이다. lastAt 은 마지막 갱신 시각(없으면 발생 시각). */
    public record Alert(String ruleId, String zoneId, String person, String severity, Instant raisedAt, Instant lastAt) { }

    /** 등급별 사건 수(위험·경고만 센다). */
    public record Counts(long critical, long high) { }

    private IncidentCounter() { }

    private static final class Chain {
        final List<Alert> alerts = new ArrayList<>();
        final Set<String> people = new HashSet<>();
        long end;
    }

    public static Counts count(List<Alert> alerts) {
        java.util.Map<String, List<Alert>> byRisk = new java.util.LinkedHashMap<>();
        for (Alert a : alerts) {
            byRisk.computeIfAbsent(a.ruleId() + "|" + (a.zoneId() == null ? "" : a.zoneId()), k -> new ArrayList<>()).add(a);
        }
        long critical = 0;
        long high = 0;
        for (List<Alert> group : byRisk.values()) {
            List<Alert> sorted = new ArrayList<>(group);
            sorted.sort(Comparator.comparing(Alert::raisedAt));
            List<Chain> chains = new ArrayList<>();
            for (Alert a : sorted) {
                long start = a.raisedAt().toEpochMilli();
                long end = a.lastAt().toEpochMilli();
                Chain found = null;
                if (a.person() != null) {
                    for (Chain c : chains) {
                        if (c.people.contains(a.person())) {
                            found = c;
                            break;
                        }
                    }
                }
                if (found == null) {
                    for (int i = chains.size() - 1; i >= 0; i--) {
                        if (start - chains.get(i).end <= MERGE_GAP_MS) {
                            found = chains.get(i);
                            break;
                        }
                    }
                }
                if (found == null) {
                    found = new Chain();
                    chains.add(found);
                    found.end = end;
                }
                found.alerts.add(a);
                found.end = Math.max(found.end, end);
                if (a.person() != null) {
                    found.people.add(a.person());
                }
            }
            for (Chain c : chains) {
                if (c.alerts.stream().anyMatch(a -> "CRITICAL".equals(a.severity()))) {
                    critical++;
                } else if (c.alerts.stream().anyMatch(a -> "HIGH".equals(a.severity()))) {
                    high++;
                }
            }
        }
        return new Counts(critical, high);
    }
}
