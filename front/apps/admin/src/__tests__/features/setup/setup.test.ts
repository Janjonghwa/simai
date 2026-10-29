import {
  coveredCells,
  coverPercent,
  formatClock,
  partialPath,
  type Point,
} from '@/features/setup/setup';
import {
  CHECK_MS,
  CHECKS,
  deriveSetup,
  headErrorPass,
  initialSetup,
  isRunning,
  setupReducer,
  TICK_MS,
  WALK_EXTRA_MS,
  WALK_MAIN_MS,
  type SetupAction,
  type SetupState,
} from '@/features/setup/setupDemo';
import { describe, expect, it } from 'vitest';

const apply = (s: SetupState, ...actions: SetupAction[]) => actions.reduce(setupReducer, s);

/** 화면처럼 TICK_MS 마다 tick 을 보낸다. */
function elapse(s: SetupState, ms: number): SetupState {
  let next = s;
  for (let t = 0; t < ms; t += TICK_MS) next = setupReducer(next, { type: 'tick', ms: TICK_MS });
  return next;
}

const camera = (s: SetupState, id: string) => deriveSetup(s).cameras.find((c) => c.key === id);

describe('경로·관측 범위 계산', () => {
  const path: Point[] = [
    [0, 0],
    [4, 0],
    [4, 4],
  ];

  it('경로의 앞쪽 일부만 돌려준다', () => {
    expect(partialPath(path, 0)).toEqual([[0, 0]]);
    expect(partialPath(path, 0.25)).toEqual([
      [0, 0],
      [2, 0],
    ]);
    expect(partialPath(path, 0.75)).toEqual([
      [0, 0],
      [4, 0],
      [4, 2],
    ]);
    expect(partialPath(path, 1)).toEqual(path);
    expect(partialPath([], 0.5)).toEqual([]);
  });

  it('지나간 자리에서 1.3m 안에 가운데가 든 1m 칸을 덮은 칸으로 본다', () => {
    expect(coveredCells([])).toEqual([]);
    // (0.5, 0.5) 에서 옆 칸 가운데는 1m, 대각선 칸은 1.41m 다.
    expect(coveredCells([[[0.5, 0.5]]])).toEqual([
      [0, 0],
      [0, 1],
      [1, 0],
    ]);
  });

  it('10×10m 교실의 덮은 비율과 경과 시각을 보인다', () => {
    expect(coverPercent(74)).toBe(74);
    expect(coverPercent(100)).toBe(100);
    expect(formatClock(5)).toBe('0:05');
    expect(formatClock(80)).toBe('1:20');
    expect(formatClock(110)).toBe('1:50');
  });

  it('머리 높이 오차는 15cm 까지 통과다', () => {
    expect(headErrorPass(11)).toBe(true);
    expect(headErrorPass(15)).toBe(true);
    expect(headErrorPass(16)).toBe(false);
  });
});

describe('보정 단계 진행', () => {
  it('처음에는 보정이 없고 cam3 각도를 고치라고 한다', () => {
    const view = deriveSetup(initialSetup);
    expect(view).toMatchObject({
      stage: 0,
      calibration: 'UNCALIBRATED',
      version: '보정 없음',
      actions: ['recheck', 'start'],
      propsSeen: [true, true, false],
    });
    expect(view.guide).toEqual({ tone: 'warn', text: expect.stringContaining('cam3') });
    expect(camera(initialSetup, 'cam3')).toEqual({
      key: 'cam3',
      tone: 'warn',
      label: '아래를 너무 봄',
      value: '2.7m · 34°',
    });
    expect(isRunning(initialSetup)).toBe(false);
  });

  it('다시 확인하면 cam3 각도가 맞고 정물이 모두 보인다', () => {
    const s = apply(initialSetup, { type: 'recheck' });
    const view = deriveSetup(s);
    expect(view.guide).toBeNull();
    expect(view.actions).toEqual(['start']);
    expect(view.propsSeen).toEqual([true, true, true]);
    expect(camera(s, 'cam3')).toMatchObject({ tone: 'ok', value: '2.7m · 27°' });
  });

  it('보정 걷기는 처음 경로를 다 걸으면 멈추고 덜 보인 구석을 알린다', () => {
    const start = apply(initialSetup, { type: 'go', stage: 1 });
    expect(isRunning(start)).toBe(true);
    expect(deriveSetup(start)).toMatchObject({
      calibration: 'PROVISIONAL',
      version: '후보 cal-8',
      actions: [],
      guide: { tone: 'info' },
    });

    const half = deriveSetup(elapse(start, WALK_MAIN_MS / 2));
    expect(half.coverPct).toBeGreaterThan(0);
    expect(half.coverPct).toBeLessThan(74);

    const s = elapse(start, WALK_MAIN_MS);
    const view = deriveSetup(s);
    expect(isRunning(s)).toBe(false);
    expect(view).toMatchObject({
      gap: true,
      walked: false,
      coverPct: 74,
      coverOk: false,
      actions: ['walkMore'],
      guide: { tone: 'warn' },
    });
    expect(formatClock(view.walkSecs)).toBe('1:20');
    // 멈춘 뒤의 tick 은 상태를 바꾸지 않는다.
    expect(setupReducer(s, { type: 'tick', ms: TICK_MS })).toBe(s);
  });

  it('이어 걸으면 통로 관측이 기준을 넘고 검증을 시작할 수 있다', () => {
    const s = elapse(
      apply(elapse(apply(initialSetup, { type: 'go', stage: 1 }), WALK_MAIN_MS), {
        type: 'walkMore',
      }),
      WALK_EXTRA_MS,
    );
    const view = deriveSetup(s);
    expect(isRunning(s)).toBe(false);
    expect(view).toMatchObject({
      gap: false,
      walked: true,
      walkProgress: 1,
      coverPct: 89,
      coverOk: true,
      actions: ['verify'],
      guide: null,
    });
    expect(formatClock(view.walkSecs)).toBe('1:50');
    expect(camera(s, 'cam1')).toMatchObject({ tone: 'active', value: '1,480' });
  });

  it('처음 경로를 다 걷기 전에는 이어 걷지 않는다', () => {
    const s = elapse(apply(initialSetup, { type: 'go', stage: 1 }), WALK_MAIN_MS / 2);
    expect(apply(s, { type: 'walkMore' })).toBe(s);
  });

  it('한 번에 긴 tick 이 와도 처음 경로 끝에서 멈춘다', () => {
    const s = apply(initialSetup, { type: 'go', stage: 1 }, { type: 'tick', ms: 1e6 });
    expect(s).toMatchObject({ walkMs: WALK_MAIN_MS, moreMs: 0 });
  });

  it('검증 지점을 차례로 재고 다 통과하면 보정을 확정할 수 있다', () => {
    const start = apply(initialSetup, { type: 'go', stage: 2 });
    expect(deriveSetup(start)).toMatchObject({
      checked: 0,
      maxErrorCm: null,
      headPass: false,
      actions: ['rewalk'],
      guide: { tone: 'info', text: '키 174cm 팀원이 1번 지점에 서 주세요' },
    });

    const one = deriveSetup(elapse(start, CHECK_MS));
    expect(one).toMatchObject({ checked: 1, maxErrorCm: 4, headPass: true, actions: ['rewalk'] });
    expect(one.guide?.text).toBe('키 174cm 팀원이 2번 지점에 서 주세요');

    const s = elapse(start, CHECK_MS * CHECKS.length);
    expect(isRunning(s)).toBe(false);
    expect(deriveSetup(s)).toMatchObject({
      checked: CHECKS.length,
      maxErrorCm: 11,
      headPass: true,
      actions: ['rewalk', 'confirm'],
      guide: null,
    });
    expect(camera(s, 'cam4')).toMatchObject({ label: '재투영 오차', value: '2.0px' });
  });

  it('확정하면 CALIBRATED 와 보정 결과를 보인다', () => {
    const view = deriveSetup(apply(initialSetup, { type: 'go', stage: 3 }));
    expect(view).toMatchObject({
      calibration: 'CALIBRATED',
      version: 'cal-8 · 10/20 14:32',
      coverPct: 89,
      checked: CHECKS.length,
      maxErrorCm: 11,
      actions: ['restart', 'zones'],
      guide: null,
      cells: [],
    });
  });

  it('단계에 다시 들어가면 그 단계를 처음부터 한다', () => {
    const fixed = apply(initialSetup, { type: 'recheck' }, { type: 'go', stage: 3 });
    expect(apply(fixed, { type: 'go', stage: 0 }).fixed).toBe(false);

    const walked = elapse(apply(initialSetup, { type: 'go', stage: 1 }), WALK_MAIN_MS);
    expect(apply(walked, { type: 'walkMore' }, { type: 'go', stage: 1 })).toMatchObject({
      walkMs: 0,
      moreMs: 0,
      more: false,
    });

    const checked = elapse(apply(initialSetup, { type: 'go', stage: 2 }), CHECK_MS * 2);
    expect(apply(checked, { type: 'go', stage: 2 }).checkMs).toBe(0);
  });
});
