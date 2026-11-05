import { boxOfDraftItem } from '@/features/setup/setup';
import type { ZoneDraftItem } from '@/features/setup/types';
import { NEW_KEY } from '@/features/zones/zoneEditModel';
import { recordZoneDraft, updateZoneDraft } from '@/store/zoneEdit.store';
import { create } from 'zustand';

// 현장 세팅 2단계 이후(세션 추적·AI 초안 수락/버리기·구역 발행 뒤 TBM 이동 연결, S15P31A101-453) 클라이언트 상태.
// 수락한 AI 초안은 발행 전까지 useZoneEditStore 의 로컬 편집 초안(NEW_KEY)으로만 옮겨지며,
// 실제 서버 반영·발행은 기존 POST /api/zones/commit 으로 수행한다.

interface SetupFlowState {
  activeSessionId: string | null;
  /** 작업 관리·세팅 화면에서 고른 작업(머리글의 '설치 · 보정'이 그 작업의 세팅을 연다). */
  workUnitId: number | null;
  acceptedDraftIds: string[];
  discardedDraftIds: string[];
  lastAcceptedCandidateId: string | null;
  zonesCommitted: boolean;
}

const INITIAL_STATE: SetupFlowState = {
  activeSessionId: null,
  workUnitId: null,
  acceptedDraftIds: [],
  discardedDraftIds: [],
  lastAcceptedCandidateId: null,
  zonesCommitted: false,
};

export const useSetupFlowStore = create<SetupFlowState>(() => ({
  ...INITIAL_STATE,
}));

/** 세팅 세션을 고른다. 다른 세션으로 바뀌면 지난 세션의 초안 수락·버리기와 구역 발행 표시를 잊는다. */
export function setActiveSetupSessionId(sessionId: string | null) {
  useSetupFlowStore.setState((state) => {
    if (sessionId === state.activeSessionId) return state;
    return {
      ...state,
      activeSessionId: sessionId,
      acceptedDraftIds: [],
      discardedDraftIds: [],
      lastAcceptedCandidateId: null,
      zonesCommitted: false,
    };
  });
}

/**
 * AI 구역 초안 하나를 수락해 구역 편집기의 로컬 초안(NEW_KEY)으로 옮긴다.
 * 발행(POST /api/zones/commit) 전까지는 로컬 초안 상태다.
 */
export function acceptSetupZoneDraft(item: ZoneDraftItem) {
  const box = boxOfDraftItem(item);
  recordZoneDraft(NEW_KEY);
  updateZoneDraft(NEW_KEY, () => ({
    patch: {
      name: item.recommended_name,
      severity: item.recommended_severity,
    },
    box,
  }));

  useSetupFlowStore.setState((state) => ({
    ...state,
    acceptedDraftIds: state.acceptedDraftIds.includes(item.candidate_id)
      ? state.acceptedDraftIds
      : [...state.acceptedDraftIds, item.candidate_id],
    discardedDraftIds: state.discardedDraftIds.filter((id) => id !== item.candidate_id),
    lastAcceptedCandidateId: item.candidate_id,
  }));
}

/** AI 구역 초안 하나를 버린다. */
export function discardSetupZoneDraft(candidateId: string) {
  useSetupFlowStore.setState((state) => ({
    ...state,
    discardedDraftIds: state.discardedDraftIds.includes(candidateId)
      ? state.discardedDraftIds
      : [...state.discardedDraftIds, candidateId],
    acceptedDraftIds: state.acceptedDraftIds.filter((id) => id !== candidateId),
  }));
}

/** 구역 발행(POST /api/zones/commit 또는 POST /api/config-bundles)이 완료되었음을 표시한다. */
export function markSetupZonesCommitted() {
  useSetupFlowStore.setState({ zonesCommitted: true });
}

/** 세팅 흐름 스토어를 초기화한다(시험·세션 초기화용). */
export function resetSetupFlowStore() {
  useSetupFlowStore.setState({ ...INITIAL_STATE });
}

/** 세팅 중인 작업을 기록한다. */
export function setSetupWorkUnitId(workUnitId: number | null) {
  useSetupFlowStore.setState({ workUnitId });
}
