import type { ReplayIndex } from '@/features/history/replayIndex';
import { formatClock, workTimeRange } from '@/features/history/works';
import type { WorkDayItem } from '@/features/work/types';
import { isEndedWork, workNameOf } from '@/features/work/workDay';
import { endedRuns, runLabel, type WorkRun } from '@/lib/workRuns';

// 지난 날짜 화면의 순수 계산: 그날 작업 고르기, 선택기 글, 실행 고르기(S15P31A101-696), 다시 보기를 시작할 시각.

/** 그날(date)의 끝난 작업(`isEndedWork`)을 시작 계획 시각순으로. 달력 칸이 보이는 작업과 같다(끝나지 않은 작업은 기록으로 치지 않는다). */
export function dayWorksOf(works: readonly WorkDayItem[], date: string): WorkDayItem[] {
  return works
    .filter((work) => work.date === date && isEndedWork(work))
    .sort((a, b) => a.from_ms - b.from_ms);
}

/** 작업 선택기 한 칸의 글: '이름 · HH:mm–HH:mm'. 이름이 비면(작업내용을 안 고른 날) '작업'. */
export function workChoiceLabel(work: WorkDayItem): string {
  return `${workNameOf(work.name)} · ${workTimeRange(work)}`;
}

/** 다시 볼 수 있는 실행(끝난 실행, 번호순). 실행 기록이 없는 옛 응답은 작업 구간 하나(실행 1)다. */
export function replayRunsOf(work: WorkDayItem): WorkRun[] {
  return endedRuns({
    runs: work.runs,
    startedAt: work.actual_started_at,
    endedAt: work.actual_ended_at,
  });
}

/**
 * 고른 실행(번호)을 찾는다. 없거나 모르는 번호면 마지막 실행(같은 날 리허설 뒤 본 시연이면 본 시연)이다.
 */
export function pickRun(runs: readonly WorkRun[], runNo: number | undefined): WorkRun | undefined {
  return runs.find((run) => run.run_no === runNo) ?? runs[runs.length - 1];
}

/**
 * 실행 하나를 다시 보기 작업으로: 실제 시작·종료를 그 실행 구간으로 바꾼다. 다시 보기 부품(useWorkReplay)은 actual_* 로 구간을 잡으므로
 * 실행 사이(관제·경보 꺼짐)는 받지 않는다. 실행이 없으면 작업 그대로다.
 */
export function runReplayWork(work: WorkDayItem, run: WorkRun | undefined): WorkDayItem {
  if (!run) return work;
  return { ...work, actual_started_at: run.started_at, actual_ended_at: run.ended_at };
}

/** 실행 선택기 한 칸의 글: '실행 1 · HH:mm–HH:mm'. */
export function runChoiceLabel(run: WorkRun): string {
  const end = run.ended_at === null ? '진행 중' : formatClock(run.ended_at);
  return `${runLabel(run)} · ${formatClock(run.started_at)}–${end}`;
}

/**
 * 처음 보일 시각: 첫 사건, 없으면 첫 기록. 구간 맨 앞은 첫 기록이 오기 전이라 CCTV 가 비므로 첫 기록 이후의 사건만 본다.
 * 기록이 하나도 없으면 null.
 */
export function firstMoment(index: ReplayIndex): number | null {
  const firstFrame = index.frames[0]?.t;
  if (firstFrame === undefined) return null;
  return index.events.find((event) => event.raisedAt >= firstFrame)?.raisedAt ?? firstFrame;
}

/** 재생 상태 중 사용자가 건드렸는지 가르는 데 쓰는 칸. */
interface PlaybackPlace {
  getStatus: () => { playhead: number | null; playing: boolean };
}

/** 우리가 옮겨 둔 자리(at)에 그대로 멈춰 있는지. 사용자가 옮기거나 재생했으면 false 다. */
export function untouched(replay: PlaybackPlace, at: number): boolean {
  const status = replay.getStatus();
  return status.playhead === at && !status.playing;
}
