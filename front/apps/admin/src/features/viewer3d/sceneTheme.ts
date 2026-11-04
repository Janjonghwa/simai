import { PALETTE, type ScenePalette } from '@/features/viewer3d/palette';
import { createViewerStore } from '@/features/viewer3d/viewer3d.store';
import { ViewerStoreContext } from '@/features/viewer3d/ViewerStoreContext';
import { useContext } from 'react';
import { useStore } from 'zustand';

// 뷰어 밖에서 장면 조각만 그릴 때(시험·배경 미리보기) 쓰는 기본값. 다크 테마다.
const NO_VIEWER = createViewerStore();

/** 지금 테마의 장면 색표. 테마가 바뀌면 이 값을 쓰는 레이어만 다시 그린다. */
export function useScenePalette(): ScenePalette {
  const store = useContext(ViewerStoreContext) ?? NO_VIEWER;
  return PALETTE[useStore(store, (s) => s.theme)];
}
