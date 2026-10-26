import type { ViewerEditBox, ViewerEditProvenance } from '@/features/viewer3d';
import { create } from 'zustand';

/** 구역 편집에서 보관한 미저장 미리보기 하나. 서버에 저장한 값이 아니다. */
export interface KeptZoneDraft {
  sourceId: string | null;
  zoneId: string;
  before: ViewerEditBox;
  after: ViewerEditBox;
  changed: boolean;
  provenance: ViewerEditProvenance;
}

export type ZoneDrafts = Readonly<Record<string, KeptZoneDraft>>;

interface ZoneDraftsState {
  drafts: ZoneDrafts;
  setDrafts: (next: ZoneDrafts | ((prev: ZoneDrafts) => ZoneDrafts)) => void;
  clear: () => void;
}

// 3D 화면(SpatialViewportHost)은 /cctv·/tbm·/system 에서 내려간다. 보관 초안은 화면보다 오래 살아야 해서 앱 세션 동안 여기에 둔다.
// 로그아웃·다른 계정·권한 변경은 bindSession 이 비운다. Source·Scene·원본 구역이 바뀐 초안은 화면이 다시 뜰 때 걸러 지운다.
// 새로고침·다른 탭으로는 넘기지 않는다(메모리에만 둔다).
export const useZoneDraftsStore = create<ZoneDraftsState>()((set) => ({
  drafts: {},
  setDrafts: (next) =>
    set((state) => ({ drafts: typeof next === 'function' ? next(state.drafts) : next })),
  clear: () => set({ drafts: {} }),
}));
