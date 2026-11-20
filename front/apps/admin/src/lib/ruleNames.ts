/** 알림·이력·보고서 공통 표시 이름(-660). 미구현 확장 규칙(R8·R10)은 제외하고 내부 rule_id(R1~R7·R9·R11)를 키로 둔다. */
export const RULE_NAME: Readonly<Record<string, string>> = {
  R1: '위험구역 진입',
  R2: '추락 위험(가장자리)',
  R3: '고소 작업면 위',
  R4: '낙상 의심',
  R5: '위험구역 접근',
  R6: '위험구역 안 시야 끊김',
  R7: '감시 저하',
  R9: '안전모 미착용',
  R11: '조끼 미착용',
};

/** 내부 rule_id → 화면·보고서 표시 번호(R1~R9). 계약·API·조건 분기에는 쓰지 않는다(-804). */
export const RULE_DISPLAY_ID: Readonly<Record<string, string>> = {
  R1: 'R1',
  R2: 'R2',
  R3: 'R3',
  R4: 'R4',
  R5: 'R5',
  R6: 'R6',
  R7: 'R7',
  R9: 'R8',
  R11: 'R9',
};

export const SEVERITY_LABEL = {
  CRITICAL: '위험',
  HIGH: '주의',
  CAUTION: '주의',
  INFO: '안내',
} as const;

export const displayRuleId = (ruleId: string) => RULE_DISPLAY_ID[ruleId] ?? ruleId;

export const ruleName = (ruleId: string) => RULE_NAME[ruleId] ?? ruleId;
