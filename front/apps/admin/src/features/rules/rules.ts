import type { Rule, RulesResponse } from '@/features/rules/types';

// 규칙 현황 표시 계산. 화면 없이 시험한다.

/** rule_profile 이 교실 시험 프로파일일 때의 값. */
const CLASSROOM_TEST = 'CLASSROOM_TEST';

/** 교실 시험 프로파일이면 시험 값을 쓰고 있다는 표시를 한다. */
export const isTestProfile = (rules: Pick<RulesResponse, 'rule_profile'>) =>
  rules.rule_profile === CLASSROOM_TEST;

/** 임계값은 규칙마다 모양이 달라(중첩 객체·목록) 칸 이름과 값을 한 줄씩 펴서 보인다. */
export function thresholdEntries(
  thresholds: Record<string, unknown> | null | undefined,
  prefix = '',
): { key: string; value: string }[] {
  if (!thresholds) return [];
  return Object.entries(thresholds).flatMap(([name, value]) => {
    const key = prefix ? `${prefix}.${name}` : name;
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      return thresholdEntries(value as Record<string, unknown>, key);
    }
    return [{ key, value: formatValue(value) }];
  });
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '없음';
  if (Array.isArray(value)) return value.map(formatValue).join(', ');
  if (typeof value === 'boolean') return value ? '켜짐' : '꺼짐';
  return String(value);
}

export interface RuleRow {
  id: string;
  name?: string;
  active: boolean;
  /** 적용 중인 판. 비활성이면 없다. */
  version?: number;
  severity?: string;
  thresholds: { key: string; value: string }[];
  /** 자기 판이 없는 규칙(R6)이 함께 적용되는 규칙. */
  appliedWith: string[];
}

/** R1~R10 순서로. 서버가 7.1 표 순서로 주지만 R 번호 순으로 다시 세운다(표 밖 규칙은 뒤). */
const ruleNumber = (id: string) => {
  const match = /^R(\d+)$/.exec(id);
  return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER;
};

export function ruleRows(rules: readonly Rule[]): RuleRow[] {
  return [...rules]
    .sort((a, b) => ruleNumber(a.rule_id) - ruleNumber(b.rule_id))
    .map((rule) => {
      const current = rule.active ? rule.current : undefined;
      return {
        id: rule.rule_id,
        name: rule.name ?? undefined,
        active: rule.active,
        version: current?.rule_version,
        severity: current?.default_severity ?? undefined,
        thresholds: thresholdEntries(current?.thresholds),
        appliedWith: rule.active ? (rule.applied_with ?? []) : [],
      };
    });
}
