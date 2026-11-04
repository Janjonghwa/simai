import type { SetupSessionResponse } from '@/features/setup/types';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import type { TbmReport } from '@/features/tbm/types';

// 작업 관리의 단계 표시줄 판정. 서버가 준 값으로만 정하고, 서버가 주지 않는 값은 지어내지 않고 'unknown'(확인 불가)으로 둔다.
//
// 단계: 세팅 → 구역 → TBM 보고서 → 시작 → 종료 → 마무리·서명
// - 세팅: work_unit.active_setup_session_id 와 그 세션(GET /api/setup-sessions/{id})의 status
// - 구역: 그 세션의 zones_committed_at(세션 시작 뒤 그 현장의 첫 구역 발행 시각) 또는 이 브라우저에서 방금 발행한 기록.
//   세션 config_version 은 세팅 보정 묶음 발행으로도 채워져 구역 발행의 근거가 아니다.
// - TBM 보고서: GET /api/reports/tbm?work_id= 의 latest(status)
// - 시작·종료: work_unit.status, actual_started_at, actual_ended_at
// - 마무리·서명: 최신 보고서의 approved_at(관리자 결재). signature_count 는 안내용.

export type StageKey = 'setup' | 'zones' | 'tbm' | 'start' | 'end' | 'sign';
/** done 끝남 · todo 아직 · running 진행 중 · failed 실패 · unknown 서버 값이 없어 확인 불가. */
export type StageState = 'done' | 'todo' | 'running' | 'failed' | 'unknown';

export interface WorkStage {
  key: StageKey;
  label: string;
  state: StageState;
  /** 상태 한 줄 설명(서버 값 그대로 또는 확인 불가 사유). */
  detail: string;
}

export const STAGE_LABEL: Record<StageKey, string> = {
  setup: '세팅',
  zones: '구역',
  tbm: 'TBM 보고서',
  start: '시작',
  end: '종료',
  sign: '마무리·서명',
};

const ORDER: readonly StageKey[] = ['setup', 'zones', 'tbm', 'start', 'end', 'sign'];

/** 세팅 세션 조회 결과. */
export type SetupSessionLookup =
  | { kind: 'none' } // 작업에 세팅 세션이 없다(active_setup_session_id 없음)
  | { kind: 'loading' }
  | { kind: 'error' }
  | { kind: 'ready'; session: SetupSessionResponse };

export interface WorkStageInput {
  task: Pick<TbmTask, 'status' | 'actualStartedAt' | 'actualEndedAt'>;
  setup: SetupSessionLookup;
  /** 이 브라우저에서 이 세션의 구역을 방금 발행했는지(useSetupFlowStore). */
  zonesCommittedHere: boolean;
  /** 최신 TBM 보고서(없으면 null). */
  report: Pick<TbmReport, 'status' | 'approved_at' | 'signature_count'> | null;
  /** 보고서 조회가 끝났는지(안 끝났으면 보고서 단계는 확인 불가). */
  reportKnown: boolean;
}

const SETUP_READY = new Set(['READY_FOR_ZONES', 'BAKING_TEXTURE', 'COMPLETED']);

function setupStage(input: WorkStageInput): WorkStage {
  const label = STAGE_LABEL.setup;
  const { setup } = input;
  if (setup.kind === 'none') return { key: 'setup', label, state: 'todo', detail: '세팅 전' };
  if (setup.kind === 'loading') return { key: 'setup', label, state: 'unknown', detail: '확인 중' };
  if (setup.kind === 'error')
    return { key: 'setup', label, state: 'unknown', detail: '확인 불가(세팅 조회 실패)' };
  const { status, progress_pct: pct } = setup.session;
  if (SETUP_READY.has(status)) return { key: 'setup', label, state: 'done', detail: '세팅 끝' };
  if (status === 'FAILED') return { key: 'setup', label, state: 'failed', detail: '세팅 실패' };
  return { key: 'setup', label, state: 'running', detail: `세팅 진행 중 ${Math.round(pct)}%` };
}

function zonesStage(input: WorkStageInput, setupDone: boolean): WorkStage {
  const label = STAGE_LABEL.zones;
  const { setup } = input;
  if (
    setup.kind === 'ready' &&
    (setup.session.zones_committed_at !== null || input.zonesCommittedHere)
  ) {
    return { key: 'zones', label, state: 'done', detail: '구역 발행됨' };
  }
  if (setup.kind === 'none') return { key: 'zones', label, state: 'todo', detail: '세팅 먼저' };
  if (setup.kind === 'ready' && !setupDone)
    return { key: 'zones', label, state: 'todo', detail: '세팅 먼저' };
  if (setup.kind === 'ready') return { key: 'zones', label, state: 'todo', detail: '구역 발행 전' };
  return {
    key: 'zones',
    label,
    state: 'unknown',
    detail: '확인 불가(세팅 조회 실패)',
  };
}

function tbmStage(input: WorkStageInput): WorkStage {
  const label = STAGE_LABEL.tbm;
  const { report } = input;
  if (!input.reportKnown) return { key: 'tbm', label, state: 'unknown', detail: '확인 중' };
  if (!report) return { key: 'tbm', label, state: 'todo', detail: '보고서 없음' };
  if (report.status === 'SUCCEEDED')
    return { key: 'tbm', label, state: 'done', detail: '보고서 있음' };
  if (report.status === 'FAILED')
    return { key: 'tbm', label, state: 'failed', detail: '보고서 생성 실패' };
  return { key: 'tbm', label, state: 'running', detail: '보고서 생성 중' };
}

function startStage(input: WorkStageInput): WorkStage {
  const label = STAGE_LABEL.start;
  const { status, actualStartedAt } = input.task;
  if (actualStartedAt || status === 'IN_PROGRESS' || status === 'DONE')
    return { key: 'start', label, state: 'done', detail: '시작함' };
  if (status === 'PLANNED') return { key: 'start', label, state: 'todo', detail: '시작 전' };
  return { key: 'start', label, state: 'unknown', detail: '확인 불가(작업 상태 없음)' };
}

function endStage(input: WorkStageInput): WorkStage {
  const label = STAGE_LABEL.end;
  const { status, actualEndedAt } = input.task;
  if (actualEndedAt || status === 'DONE')
    return { key: 'end', label, state: 'done', detail: '종료함' };
  if (status === 'IN_PROGRESS') return { key: 'end', label, state: 'running', detail: '진행 중' };
  if (status === 'PLANNED') return { key: 'end', label, state: 'todo', detail: '종료 전' };
  return { key: 'end', label, state: 'unknown', detail: '확인 불가(작업 상태 없음)' };
}

function signStage(input: WorkStageInput): WorkStage {
  const label = STAGE_LABEL.sign;
  const { report } = input;
  if (!input.reportKnown) return { key: 'sign', label, state: 'unknown', detail: '확인 중' };
  if (!report) return { key: 'sign', label, state: 'todo', detail: '보고서 없음' };
  if (report.approved_at) return { key: 'sign', label, state: 'done', detail: '관리자 결재 끝' };
  const n = report.signature_count;
  return {
    key: 'sign',
    label,
    state: 'todo',
    detail: n === undefined ? '결재 전' : `참석자 서명 ${n}명 · 결재 전`,
  };
}

export interface WorkStageResult {
  stages: readonly WorkStage[];
  /** 지금 단계(가장 뒤에 끝난 단계 다음의 첫 미완료·확인 불가 단계). 모두 끝났으면 null. */
  current: StageKey | null;
}

export function deriveWorkStages(input: WorkStageInput): WorkStageResult {
  const setup = setupStage(input);
  const stages: WorkStage[] = [
    setup,
    zonesStage(input, setup.state === 'done'),
    tbmStage(input),
    startStage(input),
    endStage(input),
    signStage(input),
  ];
  let lastDone = -1;
  stages.forEach((s, i) => {
    if (s.state === 'done') lastDone = i;
  });
  const next = stages.findIndex((s, i) => i > lastDone && s.state !== 'done');
  return { stages, current: next === -1 ? null : ORDER[next]! };
}

/** 작업 시작 전에 끝나지 않은 앞 단계(세팅·구역). 확인 불가도 끝났다고 보지 않는다. */
export function unfinishedBeforeStart(stages: readonly WorkStage[]): WorkStage[] {
  return stages.filter((s) => (s.key === 'setup' || s.key === 'zones') && s.state !== 'done');
}
