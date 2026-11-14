import type { MonitorWorks } from '@/features/monitor/useMonitorWorks';
import { WorkPpeStatus } from '@/features/monitor/WorkPpeStatus';
import {
  activePpeRequirement,
  PPE_PREPARING_TEXT,
  ppeStatusText,
  ruleProfileText,
} from '@/features/monitor/workPpeText';
import { readBundles } from '@/features/zones/useRecentBundles';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 관제 머리줄 보호구 줄(S15P31A101-654, -692). 작업 목록·묶음은 가짜로 넣는다.
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));

const state: { works: MonitorWorks; bundles: unknown[] } = {
  works: { state: 'loading' },
  bundles: [],
};
vi.mock('@/features/monitor/useMonitorWorks', () => ({ useMonitorWorks: () => state.works }));
vi.mock('@/features/zones/useRecentBundles', async (orig) => {
  const actual = await orig<typeof import('@/features/zones/useRecentBundles')>();
  return { ...actual, useRecentBundles: () => ({ data: actual.readBundles(state.bundles) }) };
});

const T = 1_760_342_400_000;

function bundle(version: string, status: string, extra: Record<string, unknown> = {}) {
  return {
    config_version: version,
    issued_at: T,
    status,
    applied_at: T,
    last_ack: null,
    items: [],
    ...extra,
  };
}

const policy = (workId: number | null, helmet: string, vest: string) => ({
  helmet,
  vest,
  work_id: workId,
  effective_from_ms: T,
});

const work = (id: number, extra: Record<string, unknown> = {}) =>
  ({ work_id: String(id), status: 'IN_PROGRESS', ...extra }) as never;

describe('workPpeText', () => {
  it('보호구 글: 적용 뒤 감시 중, 적용 전 준비 중, 끈 장비는 감시 대상 아님(개발 용어 없음)', () => {
    const both = { helmet: 'REQUIRED', vest: 'REQUIRED' } as const;
    const helmetOnly = { helmet: 'REQUIRED', vest: 'NOT_REQUIRED' } as const;
    const vestOnly = { helmet: 'NOT_REQUIRED', vest: 'REQUIRED' } as const;
    const none = { helmet: 'NOT_REQUIRED', vest: 'NOT_REQUIRED' } as const;
    expect(ppeStatusText(both, true)).toBe('안전모·조끼 감시 중');
    expect(ppeStatusText(helmetOnly, true)).toBe('안전모 감시 중 · 조끼 감시 대상 아님');
    expect(ppeStatusText(vestOnly, true)).toBe('조끼 감시 중 · 안전모 감시 대상 아님');
    expect(ppeStatusText(none, true)).toBe('보호구 감시 대상 아님');
    expect(ppeStatusText(both, false)).toBe(PPE_PREPARING_TEXT);
    expect(ppeStatusText(helmetOnly, false)).toBe('보호구 감시 준비 중 · 조끼 감시 대상 아님');
    expect(ppeStatusText(none, false)).toBe('보호구 감시 대상 아님');
  });

  it('적용 묶음이 이 작업의 요구면 그 값, 아니면 작업에 저장된 값(없으면 서버 기본 필수)', () => {
    const applied = readBundles([
      bundle('cfg-2', 'APPLIED', { ppe_policy: policy(12, 'REQUIRED', 'NOT_REQUIRED') }),
    ]);
    expect(activePpeRequirement(work(12), applied)).toEqual({
      helmet: 'REQUIRED',
      vest: 'NOT_REQUIRED',
    });
    // 시작 묶음 적용 전: 작업의 값
    expect(activePpeRequirement(work(13, { vest: 'NOT_REQUIRED' }), applied)).toEqual({
      helmet: 'REQUIRED',
      vest: 'NOT_REQUIRED',
    });
    expect(activePpeRequirement(work(13), undefined)).toEqual({
      helmet: 'REQUIRED',
      vest: 'REQUIRED',
    });
  });

  it('규칙 기준: 적용 묶음 기준, 다른 기준 묶음이 적용 대기면 덧붙인다, 모르면 null', () => {
    const classroom = bundle('cfg-1', 'APPLIED', { rule_profile: 'CLASSROOM_TEST' });
    expect(ruleProfileText(readBundles([classroom]))).toBe('교실 시험 기준 · 0.35m');
    expect(
      ruleProfileText(
        readBundles([bundle('cfg-2', 'PENDING', { rule_profile: 'OPERATION' }), classroom]),
      ),
    ).toBe('교실 시험 기준 · 0.35m (바꾼 기준 적용 대기)');
    expect(
      ruleProfileText(readBundles([bundle('cfg-1', 'APPLIED', { rule_profile: 'OPERATION' })])),
    ).toBe('운영 기준 · 2.0m');
    expect(ruleProfileText(readBundles([bundle('cfg-1', 'PENDING')]))).toBeNull();
    expect(ruleProfileText(undefined)).toBeNull();
  });
});

describe('WorkPpeStatus', () => {
  beforeEach(() => {
    state.works = { state: 'loading' };
    state.bundles = [];
  });
  afterEach(cleanup);

  it('작업 목록을 모르거나 진행 중 작업이 없으면 그리지 않는다(작업 전은 관제 상태가 맡는다)', () => {
    const { container, rerender } = render(<WorkPpeStatus />);
    expect(container.innerHTML).toBe('');
    state.works = { state: 'ready', works: [], active: null };
    state.bundles = [bundle('cfg-1', 'APPLIED', { rule_profile: 'CLASSROOM_TEST' })];
    rerender(<WorkPpeStatus />);
    expect(container.innerHTML).toBe('');
  });

  it('작업 시작 직후(이번 작업 묶음 적용 전): 바로 「보호구 감시 준비 중」, 끈 장비는 감시 대상 아님', () => {
    state.works = {
      state: 'ready',
      works: [work(12, { vest: 'NOT_REQUIRED' })],
      active: work(12, { vest: 'NOT_REQUIRED' }),
    };
    state.bundles = [
      bundle('cfg-3', 'PENDING', { ppe_policy: policy(12, 'REQUIRED', 'NOT_REQUIRED') }),
      bundle('cfg-2', 'APPLIED', {
        ppe_policy: policy(null, 'NOT_REQUIRED', 'NOT_REQUIRED'),
        rule_profile: 'CLASSROOM_TEST',
      }),
    ];
    render(<WorkPpeStatus />);
    expect(screen.getByRole('status').textContent).toBe(
      '보호구 감시 준비 중 · 조끼 감시 대상 아님',
    );
    expect(screen.getByText(/교실 시험 기준 · 0.35m/)).toBeTruthy();
  });

  it('이번 작업 묶음이 적용되면 그 요구대로 감시 중(안전모만)', () => {
    state.works = { state: 'ready', works: [work(12)], active: work(12) };
    state.bundles = [
      bundle('cfg-3', 'APPLIED', { ppe_policy: policy(12, 'REQUIRED', 'NOT_REQUIRED') }),
    ];
    render(<WorkPpeStatus />);
    expect(screen.getByRole('status').textContent).toBe('안전모 감시 중 · 조끼 감시 대상 아님');
  });
});
