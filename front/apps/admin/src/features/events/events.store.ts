import type { PanelSpan } from '@/features/events/panelBox';
import { create } from 'zustand';

interface EventsState {
  /** 이벤트 패널에서 상세로 연 이벤트. 경보 배너(모든 화면)의 「상세」와 실시간 관제 패널이 같이 쓴다. */
  openEventId: string | null;
  openEvent: (eventId: string | null) => void;
  /** 옮긴 이벤트 패널의 가로 자리. 기본 자리이거나 패널이 없으면 null. 상단 알림이 이 자리를 비킨다. */
  panelSpan: PanelSpan | null;
  setPanelSpan: (span: PanelSpan | null) => void;
}

export const useEventsStore = create<EventsState>()((set) => ({
  openEventId: null,
  openEvent: (openEventId) => set({ openEventId }),
  panelSpan: null,
  setPanelSpan: (panelSpan) => set({ panelSpan }),
}));
