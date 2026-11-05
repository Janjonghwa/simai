import type { TbmWorkUnit } from '@/features/tbm/types';

// 실시간 관제의 "오늘 작업" 계산. 작업 목록(작업 시작·종료 패널)과 대기 화면이 같은 거르기를 쓴다.
// 서버가 준 값만 쓴다: 날짜(date)와 상태(status).

/**
 * 오늘 작업 목록에 보일 작업: 오늘 날짜의 작업과, 날짜와 상관없이 지금 진행 중인 작업(어제 시작해 끝내지 않은 작업을 가리지 않는다).
 * 계획 시작 시각순이다.
 */
export function todayWorks(works: readonly TbmWorkUnit[], today: string): TbmWorkUnit[] {
  return works
    .filter((work) => work.date === today || work.status === 'IN_PROGRESS')
    .sort((a, b) => a.from_ms - b.from_ms);
}

/** 아직 할 작업(예정 또는 진행 중)이 있는지. 상태를 모르는 작업도 남은 것으로 본다(없다고 단정하지 않는다). */
export const hasRemainingWork = (works: readonly TbmWorkUnit[]): boolean =>
  works.some((work) => work.status !== 'DONE');
