import type { ProcessRow, SafetyItem } from '@/features/dashboard/dashboard';

// 대시보드의 공정 현황·안전 현황·카메라 지연은 아직 받을 곳(API)이 없어 관제 데모의 값을 그대로 둔다.
// 받을 곳이 생기면 이 파일을 지우고 훅으로 바꾼다.

/** 공정 현황(관제 데모 PROCESS). 공정명·구역은 데모 TBM 작업 T1~T3 이다. */
export const DEMO_PROCESS: readonly ProcessRow[] = [
  {
    task: 'T1',
    name: '개구부 옆 비계 설치',
    place: 'Z-04',
    plan: 60,
    actual: 55,
    status: '진행 중',
  },
  { task: 'T2', name: '자재 반입 · 정리', place: 'Z-02', plan: 80, actual: 82, status: '정상' },
  {
    task: 'T3',
    name: '바닥 마감',
    place: '북서쪽 바닥',
    plan: 40,
    actual: 28,
    status: '지연 확인',
  },
];

/** 안전 현황에 세는 사건들(검토 대기 2 · 조치 진행 중 1 · 결과 확인 대기 1 · 조치 완료 3). */
export const DEMO_SAFETY: readonly SafetyItem[] = [
  { active: true, requested: false, reported: false },
  { active: true, requested: false, reported: false },
  { active: true, requested: true, reported: false },
  { active: true, requested: true, reported: true },
  { active: false, requested: true, reported: true },
  { active: false, requested: true, reported: true },
  { active: false, requested: false, reported: false },
];

/** 안전 정보 수신 상태. */
export const DEMO_SAFETY_LINK: { lost: boolean; lastAt: number | null } = {
  lost: false,
  lastAt: null,
};

/** 카메라별 지연(ms). */
export const DEMO_CAMERA_LATENCY_MS: Readonly<Record<string, number>> = {
  cam1: 49,
  cam2: 42,
  cam3: 49,
  cam4: 42,
};
