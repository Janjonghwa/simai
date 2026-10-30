import { requestRules } from '@/features/rules/api';
import { isTestProfile, ruleRows, thresholdEntries } from '@/features/rules/rules';
import { rulesSchema, type Rule } from '@/features/rules/types';
import { createHttp } from '@/lib/http';
import { createRulesMock, rulesBody } from '@/mocks/rules';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';

const server = setupServer(...createRulesMock({ pendingConfigVersion: 'cfg-4' }).handlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const IDS = ['R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8', 'R9', 'R10'];

const rule = (change: Partial<Rule> = {}): Rule => ({
  rule_id: 'R1',
  name: '위험 구역 진입',
  stage: 'MVP',
  active: true,
  current: { rule_version: 1, profile: 'OPERATION', thresholds: { margin: 0.3 } },
  ...change,
});

describe('규칙 응답 검사', () => {
  it('서버 응답 형식을 받고 모르는 칸은 버린다', () => {
    const parsed = rulesSchema.parse({
      ...rulesBody({ pendingConfigVersion: 'cfg-4' }),
      extra: true,
    });
    expect(parsed.rule_profile).toBe('OPERATION');
    expect(parsed.config_version).toBe('cfg-3');
    expect(parsed.pending_config_version).toBe('cfg-4');
    expect(parsed.rules.map((r) => r.rule_id)).toEqual(IDS);
  });

  it('형식이 틀린 규칙 하나는 빼고 나머지를 받는다', () => {
    const parsed = rulesSchema.parse({
      rule_profile: 'OPERATION',
      config_version: null,
      pending_config_version: null,
      rules: [rule(), { rule_id: 7 }, { rule_id: 'R2', active: 'yes' }, rule({ rule_id: 'R3' })],
    });
    expect(parsed.rules.map((r) => r.rule_id)).toEqual(['R1', 'R3']);
    expect(parsed.config_version).toBeNull();
  });

  it('임계값이 객체가 아니면 그 칸만 비운다', () => {
    const parsed = rulesSchema.parse({
      rule_profile: 'OPERATION',
      rules: [
        rule({
          current: { rule_version: 2, thresholds: 'x' as unknown as Record<string, unknown> },
        }),
      ],
    });
    expect(parsed.rules[0]?.current?.rule_version).toBe(2);
    expect(parsed.rules[0]?.current?.thresholds).toBeUndefined();
  });
});

describe('GET /api/rules mock', () => {
  const http = (token?: string) =>
    createHttp({ baseUrl: 'http://api.test', getToken: () => token });

  it('로그인한 어느 역할이든 읽는다', async () => {
    for (const id of ['admin', 'foreman', 'viewer']) {
      const rules = await requestRules(http(`mock-token.${id}`));
      expect(rules.rules).toHaveLength(10);
    }
  });

  it('토큰이 없으면 401', async () => {
    await expect(requestRules(http())).rejects.toMatchObject({ status: 401 });
  });
});

describe('규칙 표시 계산', () => {
  it('교실 시험 프로파일만 시험 표시를 한다', () => {
    expect(isTestProfile({ rule_profile: 'CLASSROOM_TEST' })).toBe(true);
    expect(isTestProfile({ rule_profile: 'OPERATION' })).toBe(false);
  });

  it('임계값은 중첩 객체를 점으로 이어 펴고 값은 읽기 쉽게 바꾼다', () => {
    expect(
      thresholdEntries({
        margin: 0.3,
        r6: { severity: null, sigma_k: 2 },
        states: ['DRIFTED', 'PROVISIONAL'],
        profile: { test: true },
      }),
    ).toEqual([
      { key: 'margin', value: '0.3' },
      { key: 'r6.severity', value: '없음' },
      { key: 'r6.sigma_k', value: '2' },
      { key: 'states', value: 'DRIFTED, PROVISIONAL' },
      { key: 'profile.test', value: '켜짐' },
    ]);
    expect(thresholdEntries(null)).toEqual([]);
  });

  it('R 번호 순으로 세우고 활성 규칙만 판·등급·임계값을 싣는다', () => {
    const rows = ruleRows(rulesSchema.parse(rulesBody()).rules);
    expect(rows.map((r) => r.id)).toEqual(IDS);
    const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
    expect(byId.R1).toMatchObject({ active: true, version: 1, severity: undefined });
    expect(byId.R1?.thresholds.length).toBeGreaterThan(0);
    expect(byId.R2).toMatchObject({ active: true, version: 1, severity: 'CRITICAL' });
    // 자기 판이 없는 R6 는 활성이지만 감싼 규칙 R1 과 함께 적용된다.
    expect(byId.R6).toMatchObject({ active: true, version: undefined, appliedWith: ['R1'] });
    expect(byId.R6?.thresholds).toEqual([]);
    // 확장 규칙은 비활성이고 값이 없다.
    expect(byId.R8).toMatchObject({ active: false, version: undefined, thresholds: [] });
  });

  it('비활성 규칙은 판이 와도 값을 싣지 않고 표 밖 규칙은 뒤에 둔다', () => {
    const rows = ruleRows([
      rule({ rule_id: 'X1', active: true }),
      rule({ rule_id: 'R2', active: false }),
      rule({ rule_id: 'R1' }),
    ]);
    expect(rows.map((r) => r.id)).toEqual(['R1', 'R2', 'X1']);
    expect(rows[1]).toMatchObject({ active: false, version: undefined, thresholds: [] });
  });

  it('교실 시험 판은 R3 가 1001 판과 낮은 높이 문턱이다', () => {
    const rows = ruleRows(rulesSchema.parse(rulesBody({ profile: 'CLASSROOM_TEST' })).rules);
    const r3 = rows.find((r) => r.id === 'R3');
    expect(r3?.version).toBe(1001);
    expect(r3?.thresholds).toContainEqual({ key: 'threshold', value: '0.35' });
  });
});
