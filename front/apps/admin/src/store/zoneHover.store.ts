import { createContext } from 'react';
import { create } from 'zustand';

/** 실시간 3D 관제에서 구역 이름표를 기본 숨김(호버·선택·경보 시 표시)으로 쓸지 여부. */
export const MonitorZoneLabels = createContext(false);

interface ZoneHoverState {
  /** 마우스를 올린 위험 구역 ID(3D 구역 ↔ 왼쪽 위험 구역 목록 연동). */
  zoneId: string | null;
  setZoneId: (zoneId: string | null) => void;
}

export const useZoneHoverStore = create<ZoneHoverState>()((set) => ({
  zoneId: null,
  setZoneId: (zoneId) => set({ zoneId }),
}));
