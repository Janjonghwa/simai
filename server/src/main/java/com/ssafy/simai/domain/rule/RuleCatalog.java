package com.ssafy.simai.domain.rule;

/**
 * 명세 7.2 구현 위험 규칙 표(내부 ID R1~R7·R9·R11, 화면·보고서 표시 번호 R1~R9)의 이름·표시 번호·단계.
 * 판(rule_version)은 DB 에 있고, 이 표는 화면과 보고서가 규칙 이름·표시 번호를 통일해 쓰게 한다.
 * 미구현 확장 규칙 R8·R10 은 이번 범위에서 제외한다.
 * R6 은 다른 규칙을 감싸는 규칙이라 자기 판이 없다(감싼 규칙의 thresholds.r6, ai risk/rule_catalog.py).
 */
public enum RuleCatalog {
    R1("R1", "위험구역 진입", "MVP"),
    R2("R2", "추락 위험(가장자리)", "MVP"),
    R3("R3", "고소 작업면 위", "MVP"),
    R4("R4", "낙상 의심", "MVP"),
    R5("R5", "위험구역 접근", "MVP"),
    R6("R6", "위험구역 안 시야 끊김", "MVP"),
    R7("R7", "감시 저하", "MVP"),
    R9("R8", "안전모 미착용", "MVP"),
    R11("R9", "조끼 미착용", "MVP");

    /** 자기 판 없이 감싼 규칙의 thresholds 안에 값이 있는 규칙 */
    static final String WRAPPER = "R6";

    private final String displayId;
    private final String title;
    private final String stage;

    RuleCatalog(String displayId, String title, String stage) {
        this.displayId = displayId;
        this.title = title;
        this.stage = stage;
    }

    /** 화면·보고서 표시 번호(R1~R9). 내부 rule_id(DB·계약·API·로그)는 enum name() 을 그대로 쓴다. */
    public String displayId() {
        return displayId;
    }

    public String title() {
        return title;
    }

    String stage() {
        return stage;
    }

    /** 내부 rule_id 를 사람이 보는 표시 번호(R1~R9)로 바꾼다. R9→R8, R11→R9, 모르는 값은 그대로 둔다. */
    public static String displayIdOf(String ruleId) {
        if (ruleId == null) {
            return "";
        }
        try {
            return valueOf(ruleId).displayId();
        } catch (IllegalArgumentException e) {
            return ruleId;
        }
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
                default -> valueOf(ruleId).title();
            };
        } catch (IllegalArgumentException e) {
            return "위험";
        }
    }
}
