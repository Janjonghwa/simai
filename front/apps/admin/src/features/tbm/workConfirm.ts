import type { WorkStage } from '@/features/tbm/workStage';

// 작업 시작·종료 전의 확인창. 작업 관리와 실시간 관제의 작업 목록이 같은 문구를 쓴다.

/**
 * 시작 전 확인. 세팅·구역이 끝나지 않았어도 막지 않고 끝나지 않은 단계를 알린 뒤 묻는다(unfinishedBeforeStart 의 결과).
 * 끝나지 않은 단계가 없으면 묻지 않고 true 다.
 */
export function confirmWorkStart(unfinished: readonly WorkStage[]): boolean {
  if (unfinished.length === 0) return true;
  const names = unfinished.map((s) => `${s.label}(${s.detail})`).join(', ');
  return window.confirm(
    `아직 끝나지 않은 단계가 있습니다: ${names}\n그래도 작업을 시작하시겠습니까?`,
  );
}

/** 종료 전 확인. */
export const confirmWorkEnd = () => window.confirm('작업을 종료하시겠습니까?');

/** 작업 삭제 전 확인. */
export const confirmWorkDelete = (label: string) =>
  window.confirm(`${label} 작업을 삭제하시겠습니까?`);
