import { hasRemainingWork, todayWorks } from '@/features/monitor/todayWorks';
import type { TbmWorkUnit } from '@/features/tbm/types';
import { describe, expect, it } from 'vitest';

const TODAY = '2026-11-05';

const work = (patch: Partial<TbmWorkUnit> = {}): TbmWorkUnit => ({
  work_id: '1',
  status: 'PLANNED',
  date: TODAY,
  name: '작업',
  from_ms: 100,
  to_ms: 200,
  zone_id: null,
  place: null,
  planned_people: 1,
  lead: null,
  layout_id: null,
  work_type: null,
  ...patch,
});

describe('오늘 작업', () => {
  it('오늘 날짜의 작업과 날짜와 상관없이 진행 중인 작업만 남긴다', () => {
    const list = [
      work({ work_id: 'today' }),
      work({ work_id: 'yesterday', date: '2026-11-04' }),
      work({ work_id: 'tomorrow', date: '2026-11-06' }),
      work({ work_id: 'running-yesterday', date: '2026-11-04', status: 'IN_PROGRESS' }),
      work({ work_id: 'done-today', status: 'DONE' }),
      work({ work_id: 'done-yesterday', date: '2026-11-04', status: 'DONE' }),
    ];
    expect(
      todayWorks(list, TODAY)
        .map((w) => w.work_id)
        .sort(),
    ).toEqual(['done-today', 'running-yesterday', 'today']);
  });

  it('상태를 모르는 오늘 작업도 남는다', () => {
    expect(todayWorks([work({ status: undefined })], TODAY)).toHaveLength(1);
  });
});

describe('남은 작업(대기 화면 판정)', () => {
  it('작업이 없거나 모두 끝났으면 남은 작업이 없다', () => {
    expect(hasRemainingWork([])).toBe(false);
    expect(hasRemainingWork([work({ status: 'DONE' }), work({ status: 'DONE' })])).toBe(false);
  });

  it('예정이거나 진행 중인 작업이 하나라도 있으면 남은 작업이 있다', () => {
    expect(hasRemainingWork([work({ status: 'DONE' }), work({ status: 'PLANNED' })])).toBe(true);
    expect(hasRemainingWork([work({ status: 'IN_PROGRESS' })])).toBe(true);
  });

  it('상태를 모르는 작업은 남은 것으로 본다(없다고 단정하지 않는다)', () => {
    expect(hasRemainingWork([work({ status: undefined })])).toBe(true);
  });
});
