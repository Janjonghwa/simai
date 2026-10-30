import {
  createTask,
  nextTaskId,
  TASK_INPUT_DEFAULT,
  taskInputValid,
} from '@/features/tbm/taskInput';
import {
  evalText,
  fromMin,
  interferencesOf,
  itemRects,
  overlapsTime,
  rectAnd,
  rectHit,
  taskPeak,
  taskPlace,
  tbmInterferences,
  toMin,
} from '@/features/tbm/tbm';
import { TBM_FORM_KEY, useTbmFormStore } from '@/features/tbm/tbm.store';
import { TBM_TASKS, type TbmTask } from '@/features/tbm/tbmDemo';
import {
  attendeeRows,
  formOf,
  hasEdits,
  parseEdits,
  tbmFill,
  withEdit,
  withoutEdits,
} from '@/features/tbm/tbmForm';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const [T1, T2, T3] = TBM_TASKS as [TbmTask, TbmTask, TbmTask];

const task = (over: Partial<TbmTask>): TbmTask => ({
  id: 'T9',
  name: '시험 작업',
  from: '09:00',
  to: '10:00',
  zone: null,
  people: 1,
  lead: 'A조',
  gear: '장비 없음',
  items: [],
  evals: [],
  measures: [],
  ...over,
});

describe('시간대', () => {
  it('HH:MM 과 분을 오간다', () => {
    expect(toMin('09:30')).toBe(570);
    expect(fromMin(570)).toBe('09:30');
    expect(fromMin(toMin('00:05'))).toBe('00:05');
  });

  it('겹치는 시간대만 겹친다고 본다(끝과 시작이 맞닿으면 겹치지 않는다)', () => {
    expect(overlapsTime(T1, T2)).toBe(true);
    expect(overlapsTime(T1, T3)).toBe(false);
    expect(overlapsTime({ from: '12:00', to: '13:00' }, { from: '13:00', to: '14:00' })).toBe(
      false,
    );
  });
});

describe('배치 겹침', () => {
  it('동선은 구간마다 폭만큼 넓힌 직사각형이다', () => {
    const rects = itemRects({
      kind: 'path',
      label: '동선',
      w: 1,
      pts: [
        [0, 0],
        [0, 2],
        [3, 2],
      ],
    });
    expect(rects).toEqual([
      { x: -0.5, y: -0.5, w: 1, h: 3 },
      { x: -0.5, y: 1.5, w: 4, h: 1 },
    ]);
  });

  it('변만 닿으면 겹치지 않고, 겹친 부분을 구한다', () => {
    const a = { x: 0, y: 0, w: 2, h: 2 };
    expect(rectHit(a, { x: 2, y: 0, w: 1, h: 1 })).toBe(false);
    expect(rectHit(a, { x: 1, y: 1, w: 2, h: 2 })).toBe(true);
    expect(rectAnd(a, { x: 1, y: 1, w: 2, h: 2 })).toEqual({ x: 1, y: 1, w: 1, h: 1 });
  });
});

describe('동시 작업 간섭', () => {
  it('가짜 작업 셋에서는 T1 비계와 T2 지게차 동선이 겹치는 한 건이다', () => {
    const all = tbmInterferences(TBM_TASKS);
    expect(all).toHaveLength(1);
    const [hit] = all;
    expect(hit).toMatchObject({
      a: 'T1',
      b: 'T2',
      from: '10:00',
      to: '11:30',
      sev: 'HIGH',
      why: '비계 S1 ↔ 지게차 동선 겹침',
    });
    expect(hit!.spot!.x).toBeCloseTo(5.6);
    expect(hit!.spot!.y).toBeCloseTo(2.55);
    expect(hit!.spot!.w).toBeCloseTo(1);
    expect(hit!.spot!.h).toBeCloseTo(0.9);
    expect(interferencesOf(all, 'T2')).toHaveLength(1);
    expect(interferencesOf(all, 'T3')).toHaveLength(0);
  });

  it('배치가 안 겹쳐도 같은 구역 같은 시간대면 CAUTION, 시간대가 안 겹치면 간섭이 아니다', () => {
    const a = task({ id: 'A', zone: 'z-02', from: '09:00', to: '11:00' });
    const b = task({ id: 'B', zone: 'z-02', from: '10:00', to: '12:00' });
    const c = task({ id: 'C', zone: 'z-02', from: '11:00', to: '12:00' });
    const all = tbmInterferences([a, b, c]);
    expect(all).toEqual([
      {
        a: 'A',
        b: 'B',
        from: '10:00',
        to: '11:00',
        sev: 'CAUTION',
        why: '같은 구역 z-02에서 동시 작업',
        spot: null,
      },
      {
        a: 'B',
        b: 'C',
        from: '11:00',
        to: '12:00',
        sev: 'CAUTION',
        why: '같은 구역 z-02에서 동시 작업',
        spot: null,
      },
    ]);
  });

  it('가장 높은 평가 등급을 고르고, 평가가 없으면 null 이다', () => {
    expect(taskPeak(T1)).toBe('CRITICAL');
    expect(taskPeak(T3)).toBe('CAUTION');
    expect(taskPeak(task({}))).toBeNull();
  });

  it('구역이 없으면 적어 둔 장소를 보인다', () => {
    expect(taskPlace(T1)).toBe('개구부 구역 (z-04)');
    expect(taskPlace(T3)).toBe('북서쪽 바닥');
    expect(taskPlace(task({}))).toBe('구역 밖');
  });
});

describe('TBM 회의록 칸', () => {
  it('작업이 없으면 빈 양식이다', () => {
    const f = tbmFill(null);
    expect(f.name).toBe('');
    expect(f.same).toBeNull();
    expect(f.k0).toBeNull();
  });

  it('작업 내용과 평가·대책으로 칸을 채우고 가장 높은 위험을 중점위험으로 고른다', () => {
    const f = tbmFill(T1);
    expect(f.date).toBe('2026년 10월 16일 09:00 ~ 12:00');
    expect(f.same).toBe(true);
    expect(f.riskDone).toBe(true);
    expect(f.content).toBe('개구부 구역 (z-04) · 4명 · 반장 A조 · 장비 없음 · 배치 비계 S1 2.4m');
    expect(f.r0).toBe('비계 S1 ↔ 개구부 · R2 · ▲▲ CRITICAL · 수평 0.6m · 높이 차 2.1m');
    expect(f.c1).toBe(evalText(T1.evals[1]!));
    expect(f.m0).toBe(T1.measures[0]);
    expect(f.k0).toBe(true);
    expect(f.k2).toBeNull();
    expect(f.focusRisk).toBe(f.r0);
    expect(f.focusMeasure).toBe(T1.measures[0]);
  });

  it('참석자 서명 줄은 세 명씩, 적어도 네 줄이다', () => {
    expect(attendeeRows(2)).toBe(4);
    expect(attendeeRows(14)).toBe(5);
    expect(attendeeRows(0)).toBe(4);
  });

  it('고친 칸을 받은 내용 위에 덮고, 받은 값으로 돌리면 고친 칸에서 뺀다', () => {
    let edits = withEdit({}, 'T1', 'name', '비계 설치(수정)', T1.name);
    edits = withEdit(edits, 'T1', 'same', false, true);
    expect(formOf(T1, edits).name).toBe('비계 설치(수정)');
    expect(formOf(T1, edits).same).toBe(false);
    expect(formOf(T2, edits).name).toBe(T2.name);

    edits = withEdit(edits, 'T1', 'name', T1.name, T1.name);
    expect(edits).toEqual({ T1: { same: false } });
    edits = withEdit(edits, 'T1', 'same', true, true);
    expect(edits).toEqual({});
    expect(hasEdits(edits, 'T1')).toBe(false);
  });

  it('되돌리기는 그 작업의 고친 칸만 버린다', () => {
    const edits = { T1: { name: 'a' }, T2: { daily: 'b' } };
    expect(withoutEdits(edits, 'T1')).toEqual({ T2: { daily: 'b' } });
    expect(edits).toEqual({ T1: { name: 'a' }, T2: { daily: 'b' } });
  });

  it('남은 값에서 형식이 틀린 칸과 작업을 버린다', () => {
    expect(parseEdits(null)).toEqual({});
    expect(parseEdits([1, 2])).toEqual({});
    expect(
      parseEdits({ T1: { name: 'a', k0: true, f0: null, bad: 3, obj: {} }, T2: 'x', T3: {} }),
    ).toEqual({ T1: { name: 'a', k0: true, f0: null } });
  });
});

describe('작업 등록', () => {
  it('이름이 있고 끝이 시작보다 늦어야 한다', () => {
    expect(taskInputValid(TASK_INPUT_DEFAULT)).toBe(false);
    expect(taskInputValid({ ...TASK_INPUT_DEFAULT, name: '전기 배선' })).toBe(true);
    expect(taskInputValid({ ...TASK_INPUT_DEFAULT, name: ' ' })).toBe(false);
    expect(
      taskInputValid({ ...TASK_INPUT_DEFAULT, name: '전기 배선', from: '15:00', to: '15:00' }),
    ).toBe(false);
  });

  it('다음 번호로 만들고, 구역이 없으면 구역 밖이다', () => {
    expect(nextTaskId(TBM_TASKS)).toBe('T4');
    expect(nextTaskId([])).toBe('T1');
    const created = createTask(TBM_TASKS, {
      ...TASK_INPUT_DEFAULT,
      name: ' 전기 배선 ',
      zone: '',
      people: '0',
    });
    expect(created).toMatchObject({
      id: 'T4',
      name: '전기 배선',
      zone: null,
      area: '구역 밖',
      people: 1,
      gear: '장비 미입력',
      items: [],
      evals: [],
    });
    expect(createTask(TBM_TASKS, { ...TASK_INPUT_DEFAULT, name: 'a' })).toMatchObject({
      zone: 'z-02',
      people: 2,
    });
    expect(createTask(TBM_TASKS, { ...TASK_INPUT_DEFAULT, name: 'a' })).not.toHaveProperty('area');
  });
});

describe('useTbmFormStore', () => {
  beforeEach(() => {
    localStorage.clear();
    useTbmFormStore.setState({ edits: {} });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('칸을 고치면 브라우저에 남기고, 되돌리면 지운다', () => {
    useTbmFormStore.getState().setField('T1', 'daily', '안전대 확인', '');
    expect(useTbmFormStore.getState().edits).toEqual({ T1: { daily: '안전대 확인' } });
    const saved = JSON.parse(localStorage.getItem(TBM_FORM_KEY) ?? '{}');
    expect(saved.state.edits).toEqual({ T1: { daily: '안전대 확인' } });

    useTbmFormStore.getState().undo('T1');
    expect(useTbmFormStore.getState().edits).toEqual({});
    expect(JSON.parse(localStorage.getItem(TBM_FORM_KEY) ?? '{}').state.edits).toEqual({});
  });

  it('남은 값을 되살릴 때 형식이 틀린 칸은 버린다', async () => {
    localStorage.setItem(
      TBM_FORM_KEY,
      JSON.stringify({ state: { edits: { T2: { name: 'b', bad: 1 }, T3: 7 } }, version: 0 }),
    );
    await useTbmFormStore.persist.rehydrate();
    expect(useTbmFormStore.getState().edits).toEqual({ T2: { name: 'b' } });
  });

  it('깨진 값이 남아 있으면 비운 채로 시작한다', async () => {
    localStorage.setItem(TBM_FORM_KEY, '{not json');
    await useTbmFormStore.persist.rehydrate();
    expect(useTbmFormStore.getState().edits).toEqual({});
  });

  it('브라우저에 저장할 수 없어도 메모리에서는 고친 칸을 갖는다', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError');
    });
    expect(() => useTbmFormStore.getState().setField('T1', 'name', 'x', T1.name)).not.toThrow();
    expect(useTbmFormStore.getState().edits).toEqual({ T1: { name: 'x' } });
  });
});
