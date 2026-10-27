package com.ssafy.simai.domain.rule;

/**
 * 명세 7.1 위험 규칙 표(R1~R10)의 이름과 단계. 판(rule_version)은 DB 에 있고, 이 표는 화면이 규칙 이름과 확장 규칙(아직 없음)을
 * 보일 수 있게 한다. R6 은 다른 규칙을 감싸는 규칙이라 자기 판이 없다(감싼 규칙의 thresholds.r6, ai risk/rule_catalog.py).
 */
enum RuleCatalog {
    R1("위험 구역 진입", "MVP"),
    R2("개구부·가장자리 근접", "MVP"),
    R3("고소 위치", "MVP"),
    R4("낙상 의심", "MVP"),
    R5("진입 예측", "MVP"),
    R6("위험 구역 내 관측 상실", "MVP"),
    R7("구역 감시 능력 저하", "MVP"),
    R8("장비 근접", "EXTENSION"),
    R9("PPE 필수 구역 미착용", "EXTENSION"),
    R10("불확실 점유 근접", "EXTENSION");

    /** 자기 판 없이 감싼 규칙의 thresholds 안에 값이 있는 규칙 */
    static final String WRAPPER = "R6";

    private final String title;
    private final String stage;

    RuleCatalog(String title, String stage) {
        this.title = title;
        this.stage = stage;
    }

    String title() {
        return title;
    }

    String stage() {
        return stage;
    }
}
