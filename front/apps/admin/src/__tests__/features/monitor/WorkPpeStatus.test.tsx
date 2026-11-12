import type { MonitorWorks } from '@/features/monitor/useMonitorWorks';
import { WorkPpeStatus } from '@/features/monitor/WorkPpeStatus';
import {
  activePpeRequirement,
  IDLE_TEXT,
  ppeRequirementText,
  ruleProfileText,
  START_HINT_TEXT,
} from '@/features/monitor/workPpeText';
import { readBundles } from '@/features/zones/useRecentBundles';
import { useRealtimeStore } from '@/store/realtime.store';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 관제 머리줄 작업 상태 배지(S15P31A101-654). 작업 목록·묶음·추적 인원은 가짜로 넣는다.
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));

const navigate = vi.fn();
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => navigate }));

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

function setTracked(count: number) {
  const tracks = Array.from({ length: count }, (_, i) => ({
    world_id: `w-${i}`,
    track_lifecycle: 'CONFIRMED',
  }));
  useRealtimeStore.setState({ world: { payload: { tracks } } as never });
}

describe('workPpeText', () => {
  it('요구 글: 둘 다·하나만·둘 다 끔', () => {
    expect(ppeRequirementText({ helmet: 'REQUIRED', vest: 'REQUIRED' })).toBe('안전모·조끼 필수');
    expect(ppeRequirementText({ helmet: 'REQUIRED', vest: 'NOT_REQUIRED' })).toBe('안전모 필수');
    expect(ppeRequirementText({ helmet: 'NOT_REQUIRED', vest: 'REQUIRED' })).toBe('조끼 필수');
    expect(ppeRequirementText({ helmet: 'NOT_REQUIRED', vest: 'NOT_REQUIRED' })).toBe(
      '보호구 판정 꺼짐',
    );
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
    navigate.mockReset();
    setTracked(0);
  });
  afterEach(cleanup);

  it('작업 목록을 모르면 그리지 않는다', () => {
    const { container } = render(<WorkPpeStatus />);
    expect(container.innerHTML).toBe('');
  });

  it('작업 시작 전: 판정 꺼짐과 규칙 기준, 사람이 없으면 안내 없음', () => {
    state.works = { state: 'ready', works: [], active: null };
    state.bundles = [bundle('cfg-1', 'APPLIED', { rule_profile: 'CLASSROOM_TEST' })];
    render(<WorkPpeStatus />);
    expect(screen.getByRole('status').textContent).toContain(IDLE_TEXT);
    expect(screen.getByText(/교실 시험 기준 · 0.35m/)).toBeTruthy();
    expect(screen.queryByText(START_HINT_TEXT)).toBeNull();
  });

  it('작업 시작 전에 사람이 보이면 작업 시작 안내와 작업 관리로 가는 단추', () => {
    state.works = { state: 'ready', works: [], active: null };
    setTracked(2);
    render(<WorkPpeStatus />);
    expect(screen.getByText(START_HINT_TEXT)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '작업 관리로' }));
    expect(navigate).toHaveBeenCalledWith({ to: '/work' });
  });

  it('작업 중: 적용 묶음의 요구대로(안전모만 필수)', () => {
    state.works = { state: 'ready', works: [work(12)], active: work(12) };
    state.bundles = [
      bundle('cfg-3', 'APPLIED', { ppe_policy: policy(12, 'REQUIRED', 'NOT_REQUIRED') }),
    ];
    setTracked(1);
    render(<WorkPpeStatus />);
    expect(screen.getByRole('status').textContent).toContain('작업 진행 중 · 안전모 필수');
    expect(screen.queryByText(START_HINT_TEXT)).toBeNull();
  });
});
