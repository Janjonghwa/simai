import {
  barPercent,
  cameraCount,
  cameraLatency,
  eventsEmptyText,
  fitCount,
  planDiff,
  processSummary,
  safetyNoteText,
  stageCounts,
  ticketStage,
  type ProcessRow,
} from '@/features/dashboard/dashboard';
import { DEMO_PROCESS, DEMO_SAFETY } from '@/features/dashboard/dashboardDemo';
import { formatTime } from '@/features/events/events';
import type { CameraRow } from '@/features/system/health';
import { describe, expect, it } from 'vitest';

const row = (plan: number, actual: number): ProcessRow => ({
  task: 'T',
  name: '작업',
  place: 'Z-01',
  plan,
  actual,
  status: '정상',
});

describe('processSummary', () => {
  it('공정별 계획·실적의 평균을 반올림한다', () => {
    expect(processSummary([row(60, 55), row(80, 82), row(41, 28)])).toEqual({
      plan: 60,
      actual: 55,
    });
  });

  it('데모 공정은 계획 60% · 실적 55% 다', () => {
    expect(processSummary(DEMO_PROCESS)).toEqual({ plan: 60, actual: 55 });
  });

  it('공정이 없으면 null', () => {
    expect(processSummary([])).toBeNull();
  });
});

describe('planDiff', () => {
  it('늦으면 음수 %p 와 지연 표시', () => {
    expect(planDiff(60, 55)).toEqual({ text: '계획 대비 -5%p', behind: true });
  });

  it('앞서면 + 를 붙이고 지연이 아니다', () => {
    expect(planDiff(80, 82)).toEqual({ text: '계획 대비 +2%p', behind: false });
  });

  it('같으면 null', () => {
    expect(planDiff(50, 50)).toBeNull();
  });
});

describe('barPercent', () => {
  it('0~100 밖은 끝에 붙인다', () => {
    expect(barPercent(-5)).toBe(0);
    expect(barPercent(42)).toBe(42);
    expect(barPercent(130)).toBe(100);
  });
});

describe('ticketStage · stageCounts', () => {
  it('해소 → 조치 완료, 요청 전 → 검토 대기, 완료 보고 전 → 조치 진행 중, 보고 뒤 → 결과 확인 대기', () => {
    expect(ticketStage({ active: false, requested: true, reported: true })).toBe('done');
    expect(ticketStage({ active: true, requested: false, reported: false })).toBe('review');
    expect(ticketStage({ active: true, requested: true, reported: false })).toBe('progress');
    expect(ticketStage({ active: true, requested: true, reported: true })).toBe('verify');
  });

  it('단계마다 센다', () => {
    expect(stageCounts(DEMO_SAFETY)).toEqual({ review: 2, progress: 1, verify: 1, done: 3 });
    expect(stageCounts([])).toEqual({ review: 0, progress: 0, verify: 0, done: 0 });
  });
});

describe('safetyNoteText', () => {
  it('정상이면 정상 문구', () => {
    expect(safetyNoteText(false, null)).toBe('안전 정보 수신 정상');
  });

  it('끊기면 마지막 수신 시각을 함께 보인다', () => {
    const at = Date.UTC(2026, 9, 16, 5, 40, 12);
    expect(safetyNoteText(true, at)).toBe(`안전 정보 수신 끊김 · 마지막 수신 ${formatTime(at)}`);
    expect(safetyNoteText(true, null)).toBe('안전 정보 수신 끊김 · 마지막 수신 —');
  });
});

describe('fitCount', () => {
  it('다 들어가는 줄까지 보인다', () => {
    expect(fitCount([40, 80, 120], 130)).toBe(3);
    expect(fitCount([40, 80, 120], 100)).toBe(2);
  });

  it('1px 넘침은 들어간 것으로 본다', () => {
    expect(fitCount([40, 80, 121], 120)).toBe(3);
  });

  it('넘쳐도 두 줄은 보인다', () => {
    expect(fitCount([60, 120, 180], 50)).toBe(2);
  });

  it('줄이 min 보다 적으면 있는 만큼', () => {
    expect(fitCount([60], 10)).toBe(1);
    expect(fitCount([], 100)).toBe(0);
  });
});

describe('eventsEmptyText', () => {
  it('목록을 받았으면 사건 없음, 못 받았으면 실패, 받는 중이면 불러오는 중', () => {
    expect(eventsEmptyText(true, false)).toBe('사건 없음');
    expect(eventsEmptyText(false, true)).toBe('이벤트 목록을 받지 못했습니다');
    expect(eventsEmptyText(false, false)).toBe('불러오는 중…');
  });
});

describe('cameraCount · cameraLatency', () => {
  const cameras: CameraRow[] = [
    { id: 'cam1', ok: true, status: '정상', fps: '19.8' },
    { id: 'cam2', ok: true, status: '정상' },
    { id: 'cam3', ok: false, status: '키프레임 대기' },
    { id: 'cam9', ok: true },
  ];
  const latency = { cam1: 49, cam2: 42, cam3: 49 };

  it('정상 카메라 수 / 전체, 모르면 —', () => {
    expect(cameraCount(cameras)).toBe('3 / 4');
    expect(cameraCount([])).toBe('—');
  });

  it('정상 카메라만 지연을 보이고, 값이 없으면 비운다', () => {
    expect(cameraLatency(cameras[0]!, latency)).toBe('49ms');
    expect(cameraLatency(cameras[2]!, latency)).toBeUndefined();
    expect(cameraLatency(cameras[3]!, latency)).toBeUndefined();
  });
});
