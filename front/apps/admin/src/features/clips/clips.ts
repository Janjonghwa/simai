import type { Clip } from '@/features/clips/types';
import type { EventItem } from '@/features/events/types';

// 이벤트 클립 표시 계산(명세 5.1·11.5, FR-59·FR-99). 화면 없이 시험한다.

/** 클립은 LIVE 의 CRITICAL·HIGH 이벤트에만 만든다(계약 media_clip). */
export const hasClips = (event: EventItem) =>
  event.source === 'LIVE' && (event.severity === 'CRITICAL' || event.severity === 'HIGH');

/** CRITICAL 이벤트에 연결된 클립은 자동 보존이라 관리자가 떼지 않는다(명세 11.5). */
export const autoRetained = (event: EventItem) => event.severity === 'CRITICAL';

export const isDeleted = (clip: Clip) =>
  clip.deleted_at_ms !== null && clip.deleted_at_ms !== undefined;

export interface ClipGroup {
  /** 0 은 처음 발생, 재개마다 1씩 는다. */
  resumeCount: number;
  clips: Clip[];
}

/** 위험 구간별로 묶는다. 묶음 안은 카메라 순이다. */
export function groupClips(clips: readonly Clip[]): ClipGroup[] {
  const groups = new Map<number, Clip[]>();
  for (const clip of clips) {
    groups.set(clip.resume_count, [...(groups.get(clip.resume_count) ?? []), clip]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a - b)
    .map(([resumeCount, list]) => ({
      resumeCount,
      clips: [...list].sort((a, b) => a.camera_id.localeCompare(b.camera_id)),
    }));
}

/** 영상에 빠진 곳이 있으면 짧게 보인다. 지어내지 않고 Tier 0 가 알린 것만 보인다. */
export function clipTags(clip: Clip): string[] {
  const tags: string[] = [];
  if (clip.truncated_start) tags.push('앞 잘림');
  if (clip.truncated_end) tags.push('뒤 잘림');
  if (clip.has_gaps) tags.push('프레임 빠짐');
  return tags;
}
