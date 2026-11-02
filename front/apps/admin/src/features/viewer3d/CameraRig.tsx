import { focusPoint, moveOrbitTo } from '@/features/viewer3d/camera';
import { roomViewOf } from '@/features/viewer3d/scene/roomView';
import { useShownScanBounds } from '@/features/viewer3d/scene/useShownRoom';
import { targetKey } from '@/features/viewer3d/target';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import { roomLimitFrom, type RoomBounds } from '@/lib/roomLimit';
import { OrbitControls } from '@react-three/drei';
import { useThree } from '@react-three/fiber';
import { useCallback, useEffect, useMemo, useRef, useState, type ComponentRef } from 'react';

/**
 * 카메라 조작과 초점 요청. 카메라는 Canvas 가 살아 있는 동안 하나다.
 * 처음 시점(회전 중심·카메라 자리)은 스캔 범위에서 정하고, 스캔이 없으면 목업 교실 기준이다.
 * 새 스캔 범위가 들어오면 한 번 그 시점으로 옮기고, 시점 초기화 요청(viewResets)이 오면 그 시점으로 돌아간다.
 */
export function CameraRig() {
  const store = useViewerStore();
  const camera = useThree((s) => s.camera);
  const scene = useThree((s) => s.scene);
  const controlsRef = useRef<ComponentRef<typeof OrbitControls>>(null);
  const bounds = useShownScanBounds();
  const view = useMemo(() => roomViewOf(roomLimitFrom(bounds)), [bounds]);
  const movedForBounds = useRef<RoomBounds | null>(null);
  // 회전 중심은 처음 한 번만 넘긴다. 스캔이 사라질 때 보던 방향이 갑자기 돌지 않게 이후 이동은 아래에서 직접 한다.
  const [initialTarget] = useState(view.target);

  const goHome = useCallback(() => {
    const controls = controlsRef.current;
    if (!controls) return;
    camera.position.set(view.home[0], view.home[1], view.home[2]);
    controls.target.set(view.target[0], view.target[1], view.target[2]);
    controls.update();
  }, [camera, view]);

  useEffect(
    () =>
      store.subscribe((state, prev) => {
        if (!state.focus || state.focus === prev.focus) return;
        const controls = controlsRef.current;
        const object = scene.getObjectByName(targetKey(state.focus.target));
        if (!controls || !object) return;
        moveOrbitTo(camera.position, controls.target, focusPoint(object));
        controls.update();
      }),
    [store, scene, camera],
  );

  useEffect(
    () =>
      store.subscribe((state, prev) => {
        if (state.viewResets !== prev.viewResets) goHome();
      }),
    [store, goHome],
  );

  // 스캔이 사라졌다가 같은 범위로 돌아오면 다시 옮기지 않는다(보던 시점을 지키려고 새 범위일 때만 옮긴다).
  useEffect(() => {
    if (!bounds || movedForBounds.current === bounds) return;
    movedForBounds.current = bounds;
    goHome();
  }, [bounds, goHome]);

  return <OrbitControls ref={controlsRef} makeDefault target={initialTarget} />;
}
