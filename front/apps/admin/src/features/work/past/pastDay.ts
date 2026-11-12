import type { ReplayIndex } from '@/features/history/replayIndex';
import { workTimeRange } from '@/features/history/works';
import type { WorkDayItem } from '@/features/work/types';
import { isEndedWork, workNameOf } from '@/features/work/workDay';

// 지난 날짜 화면의 순수 계산: 그날 작업 고르기, 선택기 글, 다시 보기를 시작할 시각.

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
