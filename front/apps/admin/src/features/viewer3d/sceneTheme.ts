import { PALETTE, type ScenePalette } from '@/features/viewer3d/palette';
import { createViewerStore } from '@/features/viewer3d/viewer3d.store';
import { ViewerStoreContext } from '@/features/viewer3d/ViewerStoreContext';
import { useContext } from 'react';
import { useStore } from 'zustand';

// 뷰어 밖에서 장면 조각만 그릴 때(시험·배경 미리보기) 쓰는 기본값. 다크 테마이고 머무는 대상이 없다.
const NO_VIEWER = createViewerStore();

/** 지금 테마의 장면 색표. 테마가 바뀌면 이 값을 쓰는 레이어만 다시 그린다. */
export function useScenePalette(): ScenePalette {
  const store = useContext(ViewerStoreContext) ?? NO_VIEWER;
  return PALETTE[useStore(store, (s) => s.theme)];
}

/** 포인터가 이 대상에 HOVER_LABEL_MS 넘게 머물렀는지(이름표를 보일지). */
export function useIsHovered(key: string): boolean {
  const store = useContext(ViewerStoreContext) ?? NO_VIEWER;
  return useStore(store, (s) => s.hovered === key);
}
