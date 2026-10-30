import type { Clip } from '@/features/clips/types';
import type { EventItem } from '@/features/events/types';

// 이벤트 클립 표시 계산(명세 5.1·11.5, FR-59·FR-99). 화면 없이 시험한다.

/** 클립은 LIVE 의 CRITICAL·HIGH 이벤트에만 만든다(계약 media_clip). */
export const hasClips = (event: EventItem) =>
  event.source === 'LIVE' && (event.severity === 'CRITICAL' || event.severity === 'HIGH');

const hasTime = (ms: number | null | undefined): ms is number => ms !== null && ms !== undefined;

export const isDeleted = (clip: Clip) => hasTime(clip.deleted_at_ms);

export interface ClipGroup {
  /** 0 은 처음 발생, 재개마다 1씩 는다. */
  resumeCount: number;
  clips: Clip[];
}

/** 위험 구간별로 묶는다. 묶음 안은 카메라 순이다. */
export function groupClips(clips: readonly Clip[]): ClipGroup[] {
  const groups = new Map<number, Clip[]>();
  for (const clip of clips) {
    const list = groups.get(clip.resume_count);
    if (list) list.push(clip);
    else groups.set(clip.resume_count, [clip]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a - b)
    .map(([resumeCount, list]) => ({
      resumeCount,
      clips: list.sort((a, b) => a.camera_id.localeCompare(b.camera_id)),
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

const two = (n: number) => String(n).padStart(2, '0');

/** 월 일 시:분(이 브라우저의 시간대). Intl 은 환경마다 모양이 달라 직접 쓴다. */
const formatDay = (ms: number) => {
  const d = new Date(ms);
  return `${d.getMonth() + 1}월 ${d.getDate()}일 ${two(d.getHours())}:${two(d.getMinutes())}`;
};

/** 지운 클립의 표시. 지운 시각을 알면 함께 보인다. */
export const deletedLabel = ({ deleted_at_ms }: Clip) =>
  hasTime(deleted_at_ms) ? `삭제됨 · ${formatDay(deleted_at_ms)}` : '삭제됨';

/**
 * 보존 상태. 자동 보존(CRITICAL 이벤트 연결분, 관리자가 뗄 수 없음)은 서버가 알려 준 auto_retained 로 가린다.
 * 보존 표시가 꺼져 있으면 보존이 아니다(이벤트 등급이 CRITICAL 이어도 표시가 없으면 자동 보존으로 보지 않는다).
 */
export function retentionState(clip: Clip): 'AUTO' | 'HELD' | 'NONE' {
  if (!clip.retention_hold) return 'NONE';
  return clip.auto_retained ? 'AUTO' : 'HELD';
}
