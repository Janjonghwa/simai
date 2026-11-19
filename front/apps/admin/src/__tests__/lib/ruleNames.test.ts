import { displayRuleId, RULE_DISPLAY_ID, RULE_NAME, ruleName } from '@/lib/ruleNames';
import { describe, expect, it } from 'vitest';

describe('ruleNames', () => {
  it('미구현 확장 규칙 R8·R10 을 빼고 구현 규칙 9종만 둔다', () => {
    const implemented = ['R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R9', 'R11'];
    expect(Object.keys(RULE_NAME)).toEqual(implemented);
    expect(Object.keys(RULE_DISPLAY_ID)).toEqual(implemented);
    expect(RULE_NAME['R8']).toBeUndefined();
    expect(RULE_NAME['R10']).toBeUndefined();
    expect(ruleName('R8')).toBe('R8');
    expect(ruleName('R10')).toBe('R10');
  });

  it('화면 표시 번호만 R9→R8, R11→R9 로 이어 보이고 R1~R7 과 모르는 값은 그대로 둔다', () => {
    expect(displayRuleId('R1')).toBe('R1');
    expect(displayRuleId('R2')).toBe('R2');
    expect(displayRuleId('R3')).toBe('R3');
    expect(displayRuleId('R4')).toBe('R4');
    expect(displayRuleId('R5')).toBe('R5');
    expect(displayRuleId('R6')).toBe('R6');
    expect(displayRuleId('R7')).toBe('R7');
    expect(displayRuleId('R9')).toBe('R8');
    expect(displayRuleId('R11')).toBe('R9');
    expect(displayRuleId('R8')).toBe('R8');
    expect(displayRuleId('R10')).toBe('R10');
    expect(displayRuleId('R99')).toBe('R99');
  });

  it('규칙 이름은 내부 ID 로 안전모·조끼 미착용을 돌려준다', () => {
    expect(ruleName('R9')).toBe('안전모 미착용');
    expect(ruleName('R11')).toBe('조끼 미착용');
  });
});
