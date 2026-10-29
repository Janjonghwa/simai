import { focusPoint, moveOrbitTo } from '@/features/viewer3d/camera';
import { targetKey } from '@/features/viewer3d/target';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import type { ThreePoint } from '@/features/viewer3d/worldCoordinates';
import { OrbitControls } from '@react-three/drei';
import { useThree } from '@react-three/fiber';
import { useEffect, useRef, type ComponentRef } from 'react';

/**
 * 카메라 조작과 초점 요청. 카메라는 Canvas 가 살아 있는 동안 하나이며 시점을 다시 설정하지 않는다.
 * target: 처음 회전 중심, home: 처음 카메라 자리. 시점 초기화 요청(viewResets)이 오면 둘로 돌아간다.
 */
export function CameraRig({ target, home }: { target: ThreePoint; home: ThreePoint }) {
  const store = useViewerStore();
  const camera = useThree((s) => s.camera);
  const scene = useThree((s) => s.scene);
  const controlsRef = useRef<ComponentRef<typeof OrbitControls>>(null);

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
        if (state.viewResets === prev.viewResets) return;
        const controls = controlsRef.current;
        if (!controls) return;
        camera.position.set(home[0], home[1], home[2]);
        controls.target.set(target[0], target[1], target[2]);
        controls.update();
      }),
    [store, camera, home, target],
  );

  return <OrbitControls ref={controlsRef} makeDefault target={target} />;
}
