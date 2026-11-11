import { isAlarmSeverity, type EventEntry } from '@/features/events/entries';
import type { EventItem } from '@/features/events/types';

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

/** 관제 적용 상태 카드의 입력. 아직 모르는 값은 줄을 만들지 않는다. */
export interface AppliedStatus {
  /** 3D 모델(현재 Scene)을 받았는지. */
  sceneReady: boolean;
  /** 적용된 구역 수. 구역을 아직 못 받았으면 null. */
  zoneCount: number | null;
  /** 카메라: 전체 수와 실시간으로 이어진 수. 전체를 모르면(health 없음) total 이 0 이다. */
  cameras: { total: number; live: number };
}

/** 관제 적용 상태 줄들: '3D 모델 적용됨' · '구역 N개 적용됨' · '카메라 a/b 연결'. */
export function appliedStatusLines({ sceneReady, zoneCount, cameras }: AppliedStatus): string[] {
  const lines: string[] = [];
  if (sceneReady) lines.push('3D 모델 적용됨');
  if (zoneCount !== null) lines.push(`구역 ${zoneCount}개 적용됨`);
  if (cameras.total > 0) lines.push(`카메라 ${cameras.live}/${cameras.total} 연결`);
  return lines;
}
