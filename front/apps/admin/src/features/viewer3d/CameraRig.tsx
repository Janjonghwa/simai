import { focusPoint, moveOrbitTo } from '@/features/viewer3d/camera';
import { targetKey } from '@/features/viewer3d/target';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import { OrbitControls } from '@react-three/drei';
import { useThree } from '@react-three/fiber';
import { useEffect, useRef, type ComponentRef } from 'react';

/** 카메라 조작과 초점 요청. 카메라는 Canvas 가 살아 있는 동안 하나이며 시점을 다시 설정하지 않는다. */
export function CameraRig() {
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

  return <OrbitControls ref={controlsRef} makeDefault />;
}
