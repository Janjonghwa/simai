import { create } from 'zustand';

// 현장 세팅 2단계 이후(세션 추적·AI 초안 수락/버리기·구역 발행 뒤 TBM 이동 연결, S15P31A101-453) 클라이언트 상태.
// AI 초안 수락은 useAcceptZoneDraft 가 구역 저장(POST /api/zones/commit)으로 바로 구역을 만들고, 여기에는 수락한 초안 ID 만 남긴다.

interface SetupFlowState {
  activeSessionId: string | null;
  acceptedDraftIds: string[];
  discardedDraftIds: string[];
  zonesCommitted: boolean;
  /** 이 세션에서 맵 보강 촬영(선택)을 건너뛰었다(카드를 한 줄로 접는다, S15P31A101-607). */
  supplementSkipped: boolean;
}

const INITIAL_STATE: SetupFlowState = {
  activeSessionId: null,
  acceptedDraftIds: [],
  discardedDraftIds: [],
  zonesCommitted: false,
  supplementSkipped: false,
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
      zonesCommitted: false,
      supplementSkipped: false,
    };
  });
}

/** AI 구역 초안 하나로 구역을 만들었다(수락)고 기록한다. */
export function markSetupZoneDraftAccepted(candidateId: string) {
  useSetupFlowStore.setState((state) => ({
    ...state,
    acceptedDraftIds: state.acceptedDraftIds.includes(candidateId)
      ? state.acceptedDraftIds
      : [...state.acceptedDraftIds, candidateId],
    discardedDraftIds: state.discardedDraftIds.filter((id) => id !== candidateId),
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

/** 맵 보강 촬영을 건너뛰거나(true) 다시 연다(false). */
export function setSupplementSkipped(skipped: boolean) {
  useSetupFlowStore.setState({ supplementSkipped: skipped });
}

/** 세팅 흐름 스토어를 초기화한다(시험·세션 초기화용). */
export function resetSetupFlowStore() {
  useSetupFlowStore.setState({ ...INITIAL_STATE });
}
