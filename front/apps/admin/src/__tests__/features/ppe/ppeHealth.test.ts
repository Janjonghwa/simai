import { PPE_RECEIPT_STALE_MS, ppeReceipt, ppeRequirementMismatch } from '@/features/ppe/ppeHealth';
import { useSustained } from '@/features/ppe/usePpeReceipt';
import { readBundles } from '@/features/zones/useRecentBundles';
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// PPE 가 조용히 꺼진 상태 안내(S15P31A101-634).
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));

const T = 1_760_342_400_000;

function view(snapshotAt: number, lastReceived?: number | null) {
  return {
    tier0_source_id: 'tier0-01',
    snapshot_at_ms: snapshotAt,
    people: [],
    ...(lastReceived === undefined ? {} : { last_received_at_ms: lastReceived }),
  };
}

function bundles(...rows: [string, unknown?][]) {
  return readBundles(
    rows.map(([status, ppe], i) => ({
      config_version: `cfg-${i}`,
      issued_at: T - i,
      status,
      applied_at: status === 'APPLIED' ? T : null,
      last_ack: null,
      items: [],
      ...(ppe === undefined ? {} : { ppe_policy: ppe }),
    })),
  );
}

const REQUIRED_12 = { helmet: 'REQUIRED', vest: 'REQUIRED', work_id: 12, effective_from_ms: T };
const NOT_REQUIRED = {
  helmet: 'NOT_REQUIRED',
  vest: 'NOT_REQUIRED',
  work_id: null,
  effective_from_ms: T,
};

describe('PPE 상태 수신', () => {
  it('어느 Tier 0 든 10초 안에 받았으면 ok, 넘으면 stale', () => {
    expect(ppeReceipt([view(T, T - PPE_RECEIPT_STALE_MS)])).toBe('ok');
    expect(ppeReceipt([view(T, T - PPE_RECEIPT_STALE_MS - 1)])).toBe('stale');
    expect(ppeReceipt([view(T, T - 60_000), view(T, T - 500)])).toBe('ok');
  });

  it('받은 적 없음(null)·스냅샷 없음은 stale, 응답 전이거나 이전 EC2(칸 없음)는 unknown', () => {
    expect(ppeReceipt([view(T, null)])).toBe('stale');
    expect(ppeReceipt([])).toBe('stale');
    expect(ppeReceipt(undefined)).toBe('unknown');
    expect(ppeReceipt([view(T)])).toBe('unknown');
  });
});

describe('PPE 요구와 작업 상태', () => {
  it('작업 중인데 적용 묶음이 요구하지 않거나 다른 작업의 요구면 not_applied', () => {
    expect(ppeRequirementMismatch(bundles(['APPLIED', REQUIRED_12]), '12')).toBeNull();
    expect(ppeRequirementMismatch(bundles(['APPLIED', NOT_REQUIRED]), '12')).toBe('not_applied');
    expect(ppeRequirementMismatch(bundles(['APPLIED', REQUIRED_12]), '13')).toBe('not_applied');
    // 시작 묶음이 거부·대기면 그 전 묶음(NOT_REQUIRED)을 Tier 0 가 쓴다
    expect(
      ppeRequirementMismatch(bundles(['REJECTED', REQUIRED_12], ['APPLIED', NOT_REQUIRED]), '12'),
    ).toBe('not_applied');
  });

  it('작업이 없는데 아직 요구하면 not_released', () => {
    expect(ppeRequirementMismatch(bundles(['APPLIED', REQUIRED_12]), null)).toBe('not_released');
    expect(ppeRequirementMismatch(bundles(['APPLIED', NOT_REQUIRED]), null)).toBeNull();
  });

  it('묶음을 모르거나 ppe_policy 칸이 없거나 null(기존 PPE 요구 미수신 안내 몫)이면 판단하지 않는다', () => {
    expect(ppeRequirementMismatch(undefined, '12')).toBeNull();
    expect(ppeRequirementMismatch(bundles(['APPLIED']), '12')).toBeNull();
    expect(ppeRequirementMismatch(bundles(['APPLIED', null]), '12')).toBeNull();
  });
});

describe('useSustained', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('조건이 정한 시간 동안 이어져야 참이고, 끊기면 처음부터 다시 센다', () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ on }) => useSustained(on, 15_000), {
      initialProps: { on: true },
    });
    expect(result.current).toBe(false);
    act(() => {
      vi.advanceTimersByTime(14_000);
    });
    expect(result.current).toBe(false);
    rerender({ on: false });
    rerender({ on: true });
    act(() => {
      vi.advanceTimersByTime(14_000);
    });
    expect(result.current).toBe(false);
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(result.current).toBe(true);
    rerender({ on: false });
    expect(result.current).toBe(false);
  });
});
