import {
  PPE_NOTICE_SUSTAIN_MS,
  PPE_RECEIPT_STALE_TEXT,
  PpeWorkNotice,
} from '@/features/monitor/PpeWorkNotice';
import type { MonitorWorks } from '@/features/monitor/useMonitorWorks';
import type { PpeReceipt } from '@/features/ppe/ppeHealth';
import { readBundles } from '@/features/zones/useRecentBundles';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 작업 중 PPE 가 조용히 꺼진 상태 안내(S15P31A101-634). 작업 목록·묶음·수신은 가짜로 넣는다.
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));

const state: {
  works: MonitorWorks;
  bundles: unknown[];
  receipt: PpeReceipt;
  receiptEnabled: boolean[];
} = {
  works: { state: 'loading' },
  bundles: [],
  receipt: 'unknown',
  receiptEnabled: [],
};

vi.mock('@/features/monitor/useMonitorWorks', () => ({ useMonitorWorks: () => state.works }));
vi.mock('@/features/zones/useRecentBundles', async (orig) => {
  const actual = await orig<typeof import('@/features/zones/useRecentBundles')>();
  return { ...actual, useRecentBundles: () => ({ data: actual.readBundles(state.bundles) }) };
});
vi.mock('@/features/ppe/usePpeReceipt', async (orig) => {
  const actual = await orig<typeof import('@/features/ppe/usePpeReceipt')>();
  return {
    ...actual,
    usePpeReceipt: (enabled: boolean) => {
      state.receiptEnabled.push(enabled);
      return enabled ? state.receipt : 'unknown';
    },
  };
});

const T = 1_760_342_400_000;
const work = (id: number) => ({ work_id: String(id), status: 'IN_PROGRESS' }) as never;

function bundle(status: string, ppe: unknown) {
  return {
    config_version: `cfg-${status}`,
    issued_at: T,
    status,
    applied_at: T,
    last_ack: null,
    items: [],
    ppe_policy: ppe,
  };
}

const REQUIRED_12 = { helmet: 'REQUIRED', vest: 'REQUIRED', work_id: 12, effective_from_ms: T };
const NOT_REQUIRED = {
  helmet: 'NOT_REQUIRED',
  vest: 'NOT_REQUIRED',
  work_id: null,
  effective_from_ms: T,
};

function settle() {
  act(() => {
    vi.advanceTimersByTime(PPE_NOTICE_SUSTAIN_MS);
  });
}

describe('PpeWorkNotice', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    state.works = { state: 'loading' };
    state.bundles = [];
    state.receipt = 'unknown';
    state.receiptEnabled = [];
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('작업 중이고 요구도 맞고 상태도 들어오면 아무것도 보이지 않는다', () => {
    state.works = { state: 'ready', works: [work(12)], active: work(12) };
    state.bundles = [bundle('APPLIED', REQUIRED_12)];
    state.receipt = 'ok';
    render(<PpeWorkNotice />);
    settle();
    expect(screen.queryByRole('status')).toBeNull();
    expect(state.receiptEnabled.at(-1)).toBe(true);
  });

  it('작업 중인데 PPE 상태를 받지 못하면 15초 뒤에 안내한다', () => {
    state.works = { state: 'ready', works: [work(12)], active: work(12) };
    state.bundles = [bundle('APPLIED', REQUIRED_12)];
    state.receipt = 'stale';
    render(<PpeWorkNotice />);
    expect(screen.queryByText(PPE_RECEIPT_STALE_TEXT)).toBeNull();
    settle();
    expect(screen.getByText(PPE_RECEIPT_STALE_TEXT)).toBeTruthy();
  });

  it('이번 작업 묶음 적용 전(관제 준비 중)·작업 없음에는 PPE 상태를 보지 않는다(관제 단계·보호구 줄이 바로 알린다, -692)', () => {
    state.works = { state: 'ready', works: [work(12)], active: work(12) };
    state.bundles = [bundle('APPLIED', NOT_REQUIRED)];
    state.receipt = 'stale';
    const { unmount } = render(<PpeWorkNotice />);
    settle();
    expect(screen.queryByRole('status')).toBeNull();
    unmount();

    state.works = { state: 'ready', works: [], active: null };
    state.bundles = [bundle('APPLIED', REQUIRED_12)];
    render(<PpeWorkNotice />);
    settle();
    expect(screen.queryByRole('status')).toBeNull();
    expect(state.receiptEnabled.every((e) => !e)).toBe(true);
  });

  it('작업 목록을 모르면(받는 중) 아무것도 판단하지 않는다', () => {
    state.bundles = [bundle('APPLIED', REQUIRED_12)];
    render(<PpeWorkNotice />);
    settle();
    expect(screen.queryByRole('status')).toBeNull();
    expect(readBundles([])).toEqual([]);
  });
});
