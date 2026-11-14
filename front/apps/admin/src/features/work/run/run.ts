import { isAlarmSeverity, type EventEntry } from '@/features/events/entries';
import type { EventItem } from '@/features/events/types';
import type { MonitoringPhase } from '@/features/monitor/monitoringPhase';

// 작업 종료 단계의 순수 계산: 걸린 시간, 사건 목록, 관제 적용 상태 줄. 시계는 인자로 받는다.

/** 작업을 시작한 때부터 끝난 때(끝나지 않았으면 now)까지 걸린 시간(ms). 시계가 어긋나 음수면 0. */
export function workDurationMs(startedAt: number, endedAt: number | null, now: number): number {
  return Math.max(0, (endedAt ?? now) - startedAt);
}

/**
 * 작업 시간대의 사건(최근 것이 먼저). 실제 이벤트(LIVE) 중 위험·경고만 센다.
 * 시작 기록이 없으면 비고, 끝난 시각이 있으면 그때까지만 본다.
 */
export function workEventsOf(
  entries: readonly EventEntry[],
  startedAt: number | null,
  endedAt: number | null = null,
): EventItem[] {
  if (startedAt === null) return [];
  return entries
    .map((entry) => entry.event)
    .filter(
      (event) =>
        event.source === 'LIVE' &&
        isAlarmSeverity(event.severity) &&
        event.raised_at !== undefined &&
        event.raised_at >= startedAt &&
        (endedAt === null || event.raised_at <= endedAt),
    )
    .sort((a, b) => (b.raised_at ?? 0) - (a.raised_at ?? 0));
}

/** 관제 적용 상태 줄 하나. ok: 확인됨(초록 체크), wait: 기다리는 중, bad: 문제(경고). */
export interface AppliedLine {
  text: string;
  tone: 'ok' | 'wait' | 'bad';
}

/** 관제 적용 상태 카드의 입력. 아직 모르는 값은 줄을 만들지 않는다. */
export interface AppliedStatus {
  /** 이번 작업의 관제 단계(이번 작업 묶음의 현장 적용, S15P31A101-692). 묶음을 모르면 unknown. */
  phase: Pick<MonitoringPhase, 'stage' | 'problem'>;
  /** 3D 모델(현재 Scene)을 받았는지. */
  sceneReady: boolean;
  /** 현장이 쓰는 묶음에 실린 구역 수. 묶음을 모르면 null. 이번 작업 묶음이 적용됐을 때만 줄을 만든다. */
  appliedZoneCount: number | null;
  /** 카메라: 전체 수와 실시간으로 이어진 수. 전체를 모르면(health 없음) total 이 0 이다. */
  cameras: { total: number; live: number };
}

/** 관제 단계 줄. 모르면 null. */
function phaseLine({ stage, problem }: AppliedStatus['phase']): AppliedLine | null {
  if (problem === 'link_lost') return { text: '현장 연결 끊김 · 경보 확인 불가', tone: 'bad' };
  if (stage === 'on') return { text: '이번 작업 설정 현장 적용됨 · 경보 켜짐', tone: 'ok' };
  if (stage !== 'starting') return null;
  if (problem === 'rejected') return { text: '현장 적용 거부 · 경보 꺼짐', tone: 'bad' };
  if (problem === 'delayed') return { text: '현장 적용 지연 · 경보 꺼짐', tone: 'bad' };
  return { text: '현장 적용 대기 · 경보 꺼짐', tone: 'wait' };
}

/**
 * 관제 적용 상태 줄들: 이번 작업 설정의 현장 적용(경보 켜짐 여부) · '3D 모델 적용됨' · '구역 N개 적용됨' · '카메라 a/b 연결'.
 * 초록 체크(ok)는 확인된 것에만 준다: 구역 수는 이번 작업 묶음이 적용됐을 때 그 묶음의 구역 수, 카메라는 모두 이어졌을 때만이다
 * (S15P31A101-692, 카메라 0/4 에 체크하지 않는다).
 */
export function appliedStatusLines({
  phase,
  sceneReady,
  appliedZoneCount,
  cameras,
}: AppliedStatus): AppliedLine[] {
  const lines: AppliedLine[] = [];
  const monitoring = phaseLine(phase);
  if (monitoring) lines.push(monitoring);
  if (sceneReady) lines.push({ text: '3D 모델 적용됨', tone: 'ok' });
  if (phase.stage === 'on' && appliedZoneCount !== null) {
    lines.push({ text: `구역 ${appliedZoneCount}개 적용됨`, tone: 'ok' });
  }
  if (cameras.total > 0) {
    lines.push({
      text: `카메라 ${cameras.live}/${cameras.total} 연결`,
      tone: cameras.live === cameras.total ? 'ok' : 'bad',
    });
  }
  return lines;
}
