import { workReportOf } from '@/__tests__/features/work/workFixture';
import { DAY_PROGRESS, type DayProgress } from '@/features/work/types';
import {
  dayKind,
  dayStatusOf,
  draftWarning,
  EMPTY_WORK_NAME,
  formatDayTitle,
  isDraftPending,
  isEndingGenerating,
  pad2,
  parseDateKey,
  stageIndexOf,
  stageOfProgress,
  toDateKey,
  WORK_STAGE_IDS,
  WORK_STAGE_LABELS,
  WORK_STAGES,
  workNameOf,
} from '@/features/work/workDay';
import { describe, expect, it } from 'vitest';

describe('단계 표', () => {
  it('단계 이름은 단계 줄에 보이는 글과 같다', () => {
    expect(WORK_STAGES.map((s) => s.label)).toEqual([
      'TBM 보고서',
      '세팅',
      '작업 전 확인',
      '작업 종료',
    ]);
    expect(WORK_STAGES.map((s) => s.id)).toEqual([...WORK_STAGE_IDS]);
    expect(WORK_STAGE_LABELS.preflight).toBe('작업 전 확인');
  });

  it('서버 진행 값이 화면 단계를 정한다', () => {
    const expected: Record<DayProgress, string> = {
      REPORT: 'report',
      SETUP: 'setup',
      SETUP_DONE: 'preflight',
      IN_PROGRESS: 'run',
      ENDED: 'run',
      SUBMITTED: 'run',
    };
    for (const progress of DAY_PROGRESS) {
      expect(stageOfProgress(progress)).toBe(expected[progress]);
    }
  });

  it('단계는 앞으로만 간다(진행 순서와 단계 순서가 어긋나지 않는다)', () => {
    const indexes = DAY_PROGRESS.map((p) => stageIndexOf(stageOfProgress(p)));
    expect(indexes).toEqual([...indexes].sort((a, b) => a - b));
  });
});

describe('달력 칸 상태', () => {
  it('진행 값마다 프로토타입 dayStatus 의 글이다', () => {
    expect(DAY_PROGRESS.map((p) => dayStatusOf(p))).toEqual([
      { key: 'report', label: '보고서 작성' },
      { key: 'setting', label: '세팅 중' },
      { key: 'setup', label: '세팅 완료' },
      { key: 'running', label: '진행 중' },
      { key: 'ended', label: '작업 종료' },
      { key: 'done', label: '완료' },
    ]);
  });

  it('작업이 없거나 진행 값을 모르는 날은 칩이 없다', () => {
    expect(dayStatusOf(null)).toEqual({ key: 'none', label: '' });
    expect(dayStatusOf(undefined)).toEqual({ key: 'none', label: '' });
  });
});

describe('지난·오늘·예정', () => {
  it('날짜 키를 오늘 키와 견준다', () => {
    expect(dayKind('2026-11-10', '2026-11-11')).toBe('past');
    expect(dayKind('2026-11-11', '2026-11-11')).toBe('today');
    expect(dayKind('2026-11-12', '2026-11-11')).toBe('future');
  });

  it('달·해가 넘어가는 경계도 문자열 순서로 맞다', () => {
    expect(dayKind('2026-10-31', '2026-11-01')).toBe('past');
    expect(dayKind('2027-01-01', '2026-12-31')).toBe('future');
    expect(dayKind('2026-09-30', '2026-10-01')).toBe('past');
  });
});

describe('날짜 키', () => {
  it('Date 를 현장 시각의 YYYY-MM-DD 로 바꾼다', () => {
    expect(toDateKey(new Date(2026, 10, 11, 23, 59))).toBe('2026-11-11');
    expect(toDateKey(new Date(2026, 0, 5))).toBe('2026-01-05');
  });

  it('parseDateKey 는 그날 0시를 주고 틀린 키는 null 이다', () => {
    const date = parseDateKey('2026-11-11');
    expect(date?.getFullYear()).toBe(2026);
    expect(date?.getMonth()).toBe(10);
    expect(date?.getDate()).toBe(11);
    expect(date?.getHours()).toBe(0);
    for (const bad of [
      '',
      'x',
      '2026-11-1',
      '2026-13-01',
      '2026-02-30',
      '2026-00-10',
      '2026-11-11 ',
    ]) {
      expect(parseDateKey(bad)).toBeNull();
    }
    expect(parseDateKey('2028-02-29')).not.toBeNull();
    expect(parseDateKey('2026-02-29')).toBeNull();
  });

  it('pad2 는 한 자리 숫자만 0 으로 채운다', () => {
    expect([0, 7, 10, 59, 123].map(pad2)).toEqual(['00', '07', '10', '59', '123']);
  });

  it('날짜 화면 머리글은 YYYY년 M월 D일 (요일)', () => {
    expect(formatDayTitle('2026-11-11')).toBe('2026년 11월 11일 (수)');
    expect(formatDayTitle('2026-01-05')).toBe('2026년 1월 5일 (월)');
    // 요일은 일요일부터 센다.
    expect(formatDayTitle('2026-11-01')).toBe('2026년 11월 1일 (일)');
    expect(formatDayTitle('2026-11-07')).toBe('2026년 11월 7일 (토)');
    expect(formatDayTitle('bad')).toBe('bad');
  });
});

describe('초안을 기다리는 중인가', () => {
  const report = (overrides: Parameters<typeof workReportOf>[0]) => workReportOf(overrides);

  it('초안을 만드는 중(GENERATING)이면 기다린다', () => {
    expect(
      isDraftPending(report({ status: 'GENERATING', draft_status: 'GENERATING' }), false),
    ).toBe(true);
  });

  it('세팅이 끝났는데 서버가 아직 PENDING 이면 기다리고, 세팅 전이면 기다리지 않는다', () => {
    expect(isDraftPending(report({ status: 'PENDING', draft_status: null }), true)).toBe(true);
    expect(isDraftPending(report({ status: 'PENDING', draft_status: null }), false)).toBe(false);
  });

  it('끝난 초안(SUCCEEDED·FAILED)과 잠긴 보고서, 보고서 없음은 기다리지 않는다', () => {
    expect(isDraftPending(report({ status: 'SUCCEEDED', draft_status: 'SUCCEEDED' }), true)).toBe(
      false,
    );
    expect(isDraftPending(report({ status: 'FAILED', draft_status: 'FAILED' }), true)).toBe(false);
    expect(
      isDraftPending(
        report({ status: 'GENERATING', draft_status: 'GENERATING', locked_at: 1 }),
        true,
      ),
    ).toBe(false);
    expect(isDraftPending(null, true)).toBe(false);
    expect(isDraftPending(undefined, true)).toBe(false);
  });
});

describe('AI 초안 경고(-625)', () => {
  const report = (overrides: Parameters<typeof workReportOf>[0]) => workReportOf(overrides);

  it('초안을 만들지 않았거나(null)·실패했거나·아직 만드는 중이면 경고한다', () => {
    expect(draftWarning(report({ draft_status: null }))).toBe(
      'AI 초안을 만들지 않았습니다. 위험·대책·점검 칸이 비어 있을 수 있습니다.',
    );
    expect(draftWarning(report({ status: 'FAILED', draft_status: 'FAILED' }))).toBe(
      'AI 초안을 만들지 못했습니다. 위험·대책·점검 칸이 비어 있을 수 있습니다.',
    );
    expect(draftWarning(report({ status: 'GENERATING', draft_status: 'GENERATING' }))).toBe(
      'AI 초안을 아직 만드는 중입니다. 위험·대책·점검 칸이 비어 있을 수 있습니다.',
    );
  });

  it('초안이 끝났거나, 양식이 없거나(예전 보고서), 잠겼거나, 보고서가 없으면 경고하지 않는다', () => {
    expect(draftWarning(report({ status: 'SUCCEEDED', draft_status: 'SUCCEEDED' }))).toBeNull();
    expect(draftWarning(report({ draft_status: null, form: null }))).toBeNull();
    expect(draftWarning(report({ draft_status: null, locked_at: 1 }))).toBeNull();
    expect(draftWarning(null)).toBeNull();
    expect(draftWarning(undefined)).toBeNull();
  });
});

describe('종료 미팅을 기다리는 중인가', () => {
  const report = (overrides: Parameters<typeof workReportOf>[0]) => workReportOf(overrides);

  it('종료 미팅을 서버가 채우는 중(wrap_up_status GENERATING)이면 기다린다', () => {
    expect(isEndingGenerating(report({ wrap_up_status: 'GENERATING' }))).toBe(true);
  });

  it('끝났거나 건너뛰었거나 실패했거나 표시가 없거나 보고서가 없으면 기다리지 않는다', () => {
    for (const wrapUpStatus of ['SUCCEEDED', 'FAILED', 'SKIPPED', null]) {
      expect(isEndingGenerating(report({ wrap_up_status: wrapUpStatus }))).toBe(false);
    }
    expect(isEndingGenerating(null)).toBe(false);
    expect(isEndingGenerating(undefined)).toBe(false);
  });
});

describe('workNameOf', () => {
  it('서버 이름이 있으면 그대로, 비어 있으면(작업내용을 고르기 전) 대체어를 보인다', () => {
    expect(workNameOf('철근 배근 외 1건')).toBe('철근 배근 외 1건');
    expect(workNameOf('')).toBe(EMPTY_WORK_NAME);
    expect(EMPTY_WORK_NAME).toBe('작업');
  });
});
