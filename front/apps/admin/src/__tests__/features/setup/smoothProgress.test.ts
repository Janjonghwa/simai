import { WAITING_STAGES } from '@/features/setup/setup';
import {
  advanceProgress,
  nextBeatAfter,
  PROGRESS_BEATS,
  SMOOTH_MAX_FRACTION,
  SMOOTH_TAU_MS,
  smoothProgress,
  startTrack,
  useSmoothProgress,
} from '@/features/setup/smoothProgress';
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('smoothProgress: 서버 값 사이를 시간으로 채운다', () => {
  const tauMs = 15_000;

  it('시간이 0 이면 지난 서버 값이고, 시간이 지날수록 다음 값을 향해 단조 증가한다', () => {
    const at = (elapsedMs: number) => smoothProgress({ last: 15, nextBeat: 40, elapsedMs, tauMs });
    expect(at(0)).toBe(15);
    expect(at(tauMs)).toBeCloseTo(15 + 25 * (1 - Math.exp(-1)), 6);
    const samples = [0, 1_000, 5_000, 15_000, 30_000, 60_000].map(at);
    expect([...samples].sort((a, b) => a - b)).toEqual(samples);
  });

  it('간격의 90% 를 넘지 않는다(아주 오래 지나도 다음 서버 값 직전에서 멈춘다)', () => {
    const cap = 15 + 25 * SMOOTH_MAX_FRACTION;
    expect(SMOOTH_MAX_FRACTION).toBe(0.9);
    expect(smoothProgress({ last: 15, nextBeat: 40, elapsedMs: 10 * 60_000, tauMs })).toBeCloseTo(
      cap,
      9,
    );
    expect(smoothProgress({ last: 15, nextBeat: 40, elapsedMs: 10 * 60_000, tauMs })).toBeLessThan(
      40,
    );
    // 매우 느린 상수여도 같은 규칙: 간격 안
    expect(
      smoothProgress({ last: 65, nextBeat: 80, elapsedMs: 1e12, tauMs: 1 }),
    ).toBeLessThanOrEqual(65 + 15 * SMOOTH_MAX_FRACTION);
  });

  it('간격이 없거나 시간 상수가 0 이하이거나 시간이 음수면 지난 서버 값 그대로다', () => {
    expect(smoothProgress({ last: 40, nextBeat: 40, elapsedMs: 5_000, tauMs })).toBe(40);
    expect(smoothProgress({ last: 40, nextBeat: 30, elapsedMs: 5_000, tauMs })).toBe(40);
    expect(smoothProgress({ last: 40, nextBeat: 65, elapsedMs: 5_000, tauMs: 0 })).toBe(40);
    expect(smoothProgress({ last: 40, nextBeat: 65, elapsedMs: -5_000, tauMs })).toBe(40);
  });
});

describe('서버 진행률 끝값(beats)', () => {
  it('서버가 올리는 값은 15·40·65·80·85·95·100 이고 단계 구간 끝값을 모두 품는다', () => {
    expect(PROGRESS_BEATS).toEqual([15, 40, 65, 80, 85, 95, 100]);
    for (const stage of WAITING_STAGES) expect(PROGRESS_BEATS).toContain(stage.to);
  });

  it('값 다음에 올 끝값을 돌려주고 끝이면 null 이다', () => {
    expect(nextBeatAfter(0)).toBe(15);
    expect(nextBeatAfter(14.9)).toBe(15);
    expect(nextBeatAfter(15)).toBe(40);
    expect(nextBeatAfter(39.9)).toBe(40);
    expect(nextBeatAfter(40)).toBe(65);
    expect(nextBeatAfter(95)).toBe(100);
    expect(nextBeatAfter(100)).toBeNull();
  });

  it('보간 속도 상수는 양수 하나다', () => {
    expect(SMOOTH_TAU_MS).toBeGreaterThan(0);
  });
});

describe('advanceProgress: 표시값 규칙', () => {
  it('서버 값이 새로 오면 그 값으로 바로 바뀐다', () => {
    const track = { server: 15, since: 0, shown: 30 };
    const next = advanceProgress(track, 40, 1_000);
    expect(next).toEqual({ server: 40, since: 1_000, shown: 40 });
  });

  it('같은 서버 값이면 시간이 지날수록 오르기만 하고 다음 값 직전에서 멈춘다', () => {
    let track = startTrack(15, 0);
    let previous = track.shown;
    for (const nowMs of [100, 2_000, 9_000, 40_000, 200_000, 2_000_000]) {
      track = advanceProgress(track, 15, nowMs);
      expect(track.shown).toBeGreaterThanOrEqual(previous);
      expect(track.shown).toBeLessThanOrEqual(15 + 25 * SMOOTH_MAX_FRACTION);
      previous = track.shown;
    }
    expect(track.server).toBe(15);
  });

  it('서버 값이 내려갈 때(자동 재시도)만 표시값이 내려간다', () => {
    const track = { server: 65, since: 0, shown: 70 };
    expect(advanceProgress(track, 0, 500)).toEqual({ server: 0, since: 500, shown: 0 });
    // 같은 값·올라가는 값에서는 내려가지 않는다
    expect(advanceProgress(track, 65, 500).shown).toBeGreaterThanOrEqual(70);
  });

  it('표시값이 더 앞서 있으면 그보다 낮은 새 서버 값(끝값이 아닌 중간값)은 표시값을 내리지 않는다', () => {
    const track = { server: 40, since: 0, shown: 55 };
    const next = advanceProgress(track, 50, 1_000);
    expect(next.server).toBe(50);
    expect(next.shown).toBeGreaterThanOrEqual(55);
  });

  it('배경 수집 구간(15 미만)은 보간 없이 서버 값 그대로 지나간다', () => {
    let track = startTrack(0, 0);
    for (const [pct, nowMs] of [
      [3.5, 1_000],
      [7, 30_000],
      [14, 900_000],
    ] as const) {
      track = advanceProgress(track, pct, nowMs);
      expect(track.shown).toBe(pct);
    }
    // 오래 지나도 서버 값 이상으로 오르지 않는다
    expect(advanceProgress(track, 14, 99_000_000).shown).toBe(14);
    // 15 에 닿으면 그때부터 보간이 시작된다
    const reached = advanceProgress(track, 15, 1_000_000);
    expect(reached.shown).toBe(15);
    expect(advanceProgress(reached, 15, 1_000_000 + 20_000).shown).toBeGreaterThan(15);
  });

  it('끝(100)이면 더 오르지 않는다', () => {
    expect(advanceProgress(startTrack(100, 0), 100, 10_000_000).shown).toBe(100);
  });

  it('실패(running=false)면 새 서버 값만 받고 더 차오르지 않는다', () => {
    const track = { server: 40, since: 0, shown: 52 };
    expect(advanceProgress(track, 40, 60_000, false)).toEqual(track);
    expect(advanceProgress(track, 65, 60_000, false)).toEqual({
      server: 65,
      since: 60_000,
      shown: 65,
    });
    expect(advanceProgress(track, 0, 60_000, false).shown).toBe(0);
  });
});

describe('useSmoothProgress', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame', 'performance'] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const run = (ms: number) => act(() => vi.advanceTimersByTime(ms));

  it('서버 값 사이를 차오르다 다음 값 직전에서 멈추고, 새 값이 오면 바로 그 값이 된다', () => {
    const { result, rerender } = renderHook(({ pct }) => useSmoothProgress(pct), {
      initialProps: { pct: 15 },
    });
    expect(result.current).toBe(15);
    run(15_000);
    expect(result.current).toBeGreaterThan(25);
    expect(result.current).toBeLessThan(40);
    run(10 * 60_000);
    expect(result.current).toBeCloseTo(15 + 25 * SMOOTH_MAX_FRACTION, 1);

    rerender({ pct: 40 });
    run(50);
    expect(result.current).toBeGreaterThanOrEqual(40);
    expect(result.current).toBeLessThan(41);
  });

  it('더 올릴 값이 없으면 프레임 요청을 멈추고, 서버 값이 바뀌면 다시 돈다', () => {
    const { result, rerender } = renderHook(({ pct }) => useSmoothProgress(pct), {
      initialProps: { pct: 15 },
    });
    expect(vi.getTimerCount()).toBe(1);
    // 다음 서버 값 직전(간격의 90%)에 닿으면 멈춘다.
    run(10 * 60_000);
    expect(result.current).toBeCloseTo(15 + 25 * SMOOTH_MAX_FRACTION, 1);
    expect(vi.getTimerCount()).toBe(0);

    rerender({ pct: 40 });
    expect(vi.getTimerCount()).toBe(1);
    run(10 * 60_000);
    expect(vi.getTimerCount()).toBe(0);

    // 끝(100)과 배경 수집 구간(15 미만)은 서버 값을 한 번 그린 뒤 멈춘다.
    rerender({ pct: 100 });
    run(50);
    expect(result.current).toBe(100);
    expect(vi.getTimerCount()).toBe(0);
    rerender({ pct: 7 });
    run(50);
    expect(result.current).toBe(7);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('서버 값이 내려가면(자동 재시도) 표시값도 내려간다', () => {
    const { result, rerender } = renderHook(({ pct }) => useSmoothProgress(pct), {
      initialProps: { pct: 65 },
    });
    run(20_000);
    expect(result.current).toBeGreaterThan(65);
    rerender({ pct: 0 });
    run(50);
    expect(result.current).toBe(0);
  });

  it('running 이 false 이면 더 차오르지 않는다', () => {
    const { result, rerender } = renderHook(({ pct, running }) => useSmoothProgress(pct, running), {
      initialProps: { pct: 40, running: true },
    });
    run(10_000);
    const stopped = result.current;
    expect(stopped).toBeGreaterThan(40);
    rerender({ pct: 40, running: false });
    run(60_000);
    expect(result.current).toBe(stopped);
  });
});
