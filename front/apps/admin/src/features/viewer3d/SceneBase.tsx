import type { ViewerMode } from '@/features/viewer3d/types';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import { WorldScene } from '@/features/viewer3d/WorldScene';
import { useStore } from 'zustand';

// 기준 장면: 조명·1m 격자·World 바닥과 벽 경계. 격자 선은 World 원점(Three 원점)을 지나는 1m 간격이다.
// WebGL 재질은 CSS 토큰을 바로 읽지 못해 장면 전용 값을 여기에만 둔다.
const GRID_ARGS: [number, number, number, number] = [20, 20, 0x8a8f98, 0x4a4f58];

// 모드마다 기준 격자 농도를 달리해 실시간 화면과 구분한다. 모드별 장면 내용은 후속 티켓에서 붙인다.
const GRID_OPACITY: Record<ViewerMode, number> = { LIVE: 0.5, PLANNED: 0.3, REPLAY: 0.2 };

export function SceneBase() {
  const mode = useStore(useViewerStore(), (s) => s.mode);

  return (
    <>
      <ambientLight intensity={0.6} />
      <directionalLight position={[5, 10, 5]} intensity={1} />
      <gridHelper args={GRID_ARGS} material-transparent material-opacity={GRID_OPACITY[mode]} />
      <WorldScene />
    </>
  );
}
