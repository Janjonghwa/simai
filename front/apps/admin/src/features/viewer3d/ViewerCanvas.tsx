import { CameraRig } from '@/features/viewer3d/CameraRig';
import { SceneBase } from '@/features/viewer3d/SceneBase';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import { Canvas, useThree } from '@react-three/fiber';
import { memo, useCallback, useEffect, type ReactNode } from 'react';

// 모듈 상수로 둬서 Canvas 가 다시 렌더돼도 카메라·렌더러 설정을 다시 적용하지 않는다.
const CAMERA = { position: [8, 8, 8] as [number, number, number], fov: 50, near: 0.1, far: 500 };
const GL = { antialias: true, alpha: true };
const DPR: [number, number] = [1, 2];

/**
 * 렌더러 준비와 WebGL 컨텍스트 회수·복구를 상태로 알린다.
 * 준비는 컨텍스트가 살아 있는 채로 렌더러가 실제 프레임을 그린 뒤에만 알린다(복구 이벤트만으로 판단하지 않는다).
 */
function RendererStatus() {
  const store = useViewerStore();
  const gl = useThree((s) => s.gl);

  useEffect(() => {
    const canvas = gl.domElement;
    const context = gl.getContext();
    const { markReady, markError } = store.getState();
    let frameRequest = 0;

    // three 는 컨텍스트를 잃은 동안 render() 를 건너뛰어 프레임 수가 늘지 않는다.
    const confirmRendered = () => {
      cancelAnimationFrame(frameRequest);
      const start = gl.info.render.frame;
      const check = () => {
        if (context.isContextLost()) return;
        if (gl.info.render.frame > start) markReady();
        else frameRequest = requestAnimationFrame(check);
      };
      frameRequest = requestAnimationFrame(check);
    };
    const handleLost = (event: Event) => {
      // 기본 동작을 막아야 브라우저가 컨텍스트를 돌려준다.
      event.preventDefault();
      cancelAnimationFrame(frameRequest);
      markError({ code: 'CONTEXT_LOST', message: 'WebGL 컨텍스트를 잃었다.' });
    };

    confirmRendered();
    canvas.addEventListener('webglcontextlost', handleLost);
    canvas.addEventListener('webglcontextrestored', confirmRendered);
    return () => {
      cancelAnimationFrame(frameRequest);
      canvas.removeEventListener('webglcontextlost', handleLost);
      canvas.removeEventListener('webglcontextrestored', confirmRendered);
    };
  }, [store, gl]);

  return null;
}

interface ViewerCanvasProps {
  children?: ReactNode;
}

/** 뷰어의 단일 Canvas. 부모 크기를 따라가고, 입력은 Canvas 안에서만 받는다. */
export const ViewerCanvas = memo(function ViewerCanvas({ children }: ViewerCanvasProps) {
  const store = useViewerStore();
  // 빈 곳을 클릭하면 선택을 푼다. 끌어서 시점을 돌린 것은 클릭으로 치지 않는다.
  const handlePointerMissed = useCallback(() => {
    store.getState().clearSelection();
  }, [store]);

  return (
    <Canvas camera={CAMERA} gl={GL} dpr={DPR} onPointerMissed={handlePointerMissed}>
      <RendererStatus />
      <CameraRig />
      <SceneBase />
      {children}
    </Canvas>
  );
});
