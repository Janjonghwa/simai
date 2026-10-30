import { isEmptyDraft, type TargetDraft } from '@/features/zones/zoneEditModel';
import { create } from 'zustand';

type Drafts = Readonly<Record<string, TargetDraft>>;

interface ZoneEditState {
  /** 구역 ID(또는 새 구역 키)마다 저장 전 초안. 서버에 저장한 값이 아니다. */
  drafts: Drafts;
  /** 초안 하나를 고친다. 고친 것이 하나도 없는 초안은 지운다. */
  update: (key: string, next: (prev: TargetDraft | undefined) => TargetDraft) => void;
  remove: (key: string) => void;
  clear: () => void;
}

const omit = (drafts: Drafts, key: string): Drafts =>
  Object.fromEntries(Object.entries(drafts).filter(([id]) => id !== key));

// 구역 편집(/zones)은 /cctv·/tbm 으로 나갔다 와도 고치던 초안이 남아야 해서 앱 세션 동안 여기에 둔다(메모리에만 둔다).
// 로그아웃·다른 계정·권한 변경은 bindSession 이 clearZoneDrafts 로 비운다.
export const useZoneEditStore = create<ZoneEditState>()((set) => ({
  drafts: {},
  update: (key, next) =>
    set((state) => {
      const value = next(state.drafts[key]);
      if (!isEmptyDraft(value)) return { drafts: { ...state.drafts, [key]: value } };
      return key in state.drafts ? { drafts: omit(state.drafts, key) } : state;
    }),
  remove: (key) =>
    set((state) => (key in state.drafts ? { drafts: omit(state.drafts, key) } : state)),
  clear: () => set({ drafts: {} }),
}));

/** 사용자가 끝나거나 바뀌면(bindSession onUserChange) 보관한 초안을 모두 지운다. */
export const clearZoneDrafts = () => useZoneEditStore.getState().clear();

/** 컴포넌트 밖(이벤트 처리기·비동기 뒤)에서 초안 하나를 고친다. */
export const updateZoneDraft = (
  key: string,
  next: (prev: TargetDraft | undefined) => TargetDraft,
) => useZoneEditStore.getState().update(key, next);

/** 컴포넌트 밖에서 초안 하나를 버린다. */
export const removeZoneDraft = (key: string) => useZoneEditStore.getState().remove(key);
