package com.ssafy.simai.domain.rule;

/**
 * 명세 7.2 위험 규칙 표(R1~R11)의 이름과 단계. 판(rule_version)은 DB 에 있고, 이 표는 화면이 규칙 이름과 확장 규칙(아직 없음)을
 * 보일 수 있게 한다. R6 은 다른 규칙을 감싸는 규칙이라 자기 판이 없다(감싼 규칙의 thresholds.r6, ai risk/rule_catalog.py).
 */
public enum RuleCatalog {
    R1("위험구역 진입", "MVP"),
    R2("추락 위험(가장자리)", "MVP"),
    R3("고소 작업면 위", "MVP"),
    R4("낙상 의심", "MVP"),
    R5("위험구역 접근", "MVP"),
    R6("위험구역 안 시야 끊김", "MVP"),
    R7("감시 저하", "MVP"),
    R8("장비 근접", "EXTENSION"),
    R9("안전모 미착용", "MVP"),
    R10("불확실 점유 근접", "EXTENSION"),
    R11("조끼 미착용", "MVP");

    /** 자기 판 없이 감싼 규칙의 thresholds 안에 값이 있는 규칙 */
    static final String WRAPPER = "R6";

    private final String title;
    private final String stage;

    RuleCatalog(String title, String stage) {
        this.title = title;
        this.stage = stage;
    }

    public String title() {
        return title;
    }

    String stage() {
        return stage;
    }

    public static String titleOf(String ruleId) {
        if (ruleId == null) {
            return "";
        }
        try {
            return valueOf(ruleId).title();
        } catch (IllegalArgumentException e) {
            return ruleId;
        }
    }

    /** I4 word 상한 8자를 넘는 규칙만 줄인다. 계약은 바꾸지 않는다. */
    public static String watchWord(String ruleId) {
        if (ruleId == null) {
            return "위험";
        }
        try {
            return switch (valueOf(ruleId)) {
                case R2 -> "추락 위험";
                case R6 -> "시야 끊김";
                case R10 -> "점유 근접";
                default -> valueOf(ruleId).title();
            };
        } catch (IllegalArgumentException e) {
            return "위험";
        }
    }
}
