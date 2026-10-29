import { formatTime } from '@/features/events/events';
import type { CameraRow } from '@/features/system/health';

// 대시보드(관제 데모 기준 공정 · 안전 · 이벤트 · 현장 도면 · 카메라)가 보일 값을 만든다. 화면 컴포넌트 없이 시험한다.

export type ProcessStatus = '진행 중' | '정상' | '지연 확인';

/** 공정 한 줄. plan·actual 은 %다. */
export interface ProcessRow {
  task: string;
  name: string;
  /** 구역 ID, 구역이 없으면 작업 장소. */
  place: string;
  plan: number;
  actual: number;
  status: ProcessStatus;
}

/** 전체 공정 진행률: 공정별 계획·실적의 평균(반올림). 공정이 없으면 null. */
export function processSummary(
  rows: readonly ProcessRow[],
): { plan: number; actual: number } | null {
  if (rows.length === 0) return null;
  const avg = (key: 'plan' | 'actual') =>
    Math.round(rows.reduce((sum, row) => sum + row[key], 0) / rows.length);
  return { plan: avg('plan'), actual: avg('actual') };
}

/** 계획 대비 실적 차이(%p). 같으면 null. behind 면 지연이라 주황으로 보인다. */
export function planDiff(plan: number, actual: number): { text: string; behind: boolean } | null {
  const diff = actual - plan;
  if (diff === 0) return null;
  return { text: `계획 대비 ${diff > 0 ? '+' : ''}${diff}%p`, behind: diff < 0 };
}

/** 막대 길이(%). 0~100 밖은 끝에 붙인다. */
export const barPercent = (value: number) => Math.min(Math.max(value, 0), 100);

/** 조치 업무 단계(위험 등급과 따로 센다). */
export type TicketStage = 'review' | 'progress' | 'verify' | 'done';

/** 안전 현황에 세는 사건 하나. 시스템 이벤트·경보 전 안내는 넣지 않는다. */
export interface SafetyItem {
  /** 아직 해소되지 않았다. */
  active: boolean;
  /** 반장에게 조치를 요청했다. */
  requested: boolean;
  /** 반장이 완료(사진)를 보고했다. */
  reported: boolean;
}

export const TICKET_STAGES: readonly { stage: TicketStage; label: string }[] = [
  { stage: 'review', label: '검토 대기' },
  { stage: 'progress', label: '조치 진행 중' },
  { stage: 'verify', label: '결과 확인 대기' },
  { stage: 'done', label: '조치 완료' },
];

export function ticketStage(item: SafetyItem): TicketStage {
  if (!item.active) return 'done';
  if (!item.requested) return 'review';
  return item.reported ? 'verify' : 'progress';
}

export function stageCounts(items: readonly SafetyItem[]): Record<TicketStage, number> {
  const counts: Record<TicketStage, number> = { review: 0, progress: 0, verify: 0, done: 0 };
  for (const item of items) counts[ticketStage(item)] += 1;
  return counts;
}

/** 안전 정보 수신 문구. 끊겼으면 마지막으로 받은 시각을 함께 보인다. */
export const safetyNoteText = (lost: boolean, lastAt: number | null) =>
  lost ? `안전 정보 수신 끊김 · 마지막 수신 ${formatTime(lastAt)}` : '안전 정보 수신 정상';

/**
 * 카드에 다 들어가는 줄 수. bottoms 는 줄마다 목록 위에서 그 줄 아래 끝까지(px)다.
 * 넘쳐도 min 줄은 보인다(관제 데모: 2~3개).
 */
export function fitCount(bottoms: readonly number[], height: number, min = 2): number {
  const over = bottoms.findIndex((bottom) => bottom > height + 1);
  const fits = over === -1 ? bottoms.length : over;
  return Math.min(bottoms.length, Math.max(min, fits));
}

/** 이벤트가 없을 때의 문구. */
export function eventsEmptyText(loaded: boolean, failed: boolean): string {
  if (loaded) return '사건 없음';
  return failed ? '이벤트 목록을 받지 못했습니다' : '불러오는 중…';
}

/** 머리줄의 정상 카메라 수(정상 / 전체). 상태를 모르면 —. */
export const cameraCount = (rows: readonly CameraRow[]) =>
  rows.length === 0 ? '—' : `${rows.filter((row) => row.ok).length} / ${rows.length}`;

/** 카메라 지연 칸. 정상이 아닌 카메라는 비운다. */
export function cameraLatency(
  row: CameraRow,
  latencyMs: Readonly<Record<string, number>>,
): string | undefined {
  const value = row.ok ? latencyMs[row.id] : undefined;
  return value === undefined ? undefined : `${value}ms`;
}
