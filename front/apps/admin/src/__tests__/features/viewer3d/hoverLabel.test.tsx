import { HOVER_LABEL_MS, Selectable } from '@/features/viewer3d/Selectable';
import { createViewerStore, type ViewerStore } from '@/features/viewer3d/viewer3d.store';
import { ViewerStoreContext } from '@/features/viewer3d/ViewerStoreContext';
import { createRoot, extend, useThree, type ReconcilerRoot } from '@react-three/fiber';
import { act, useEffect } from 'react';
import * as THREE from 'three';
import { type Scene } from 'three';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 3D 이름표는 포인터가 같은 대상에 HOVER_LABEL_MS 머물 때만 보인다. 실제 광선 피킹은 브라우저에서 확인한다.

extend(THREE as unknown as Parameters<typeof extend>[0]);
Reflect.set(globalThis, 'IS_REACT_ACT_ENVIRONMENT', true);

const Z1 = { type: 'zone', id: 'z-01' } as const;
const Z2 = { type: 'zone', id: 'z-02' } as const;

function fakeRenderer(canvas: HTMLCanvasElement) {
  return {
    render: () => {},
    setPixelRatio: () => {},
    setSize: () => {},
    dispose: () => {},
    forceContextLoss: () => {},
    domElement: canvas,
    shadowMap: { enabled: false, type: 0, needsUpdate: false },
    xr: {
      enabled: false,
      isPresenting: false,
      addEventListener: () => {},
      removeEventListener: () => {},
      setAnimationLoop: () => {},
    },
    renderLists: { dispose: () => {} },
    outputColorSpace: '',
    toneMapping: 0,
  };
}

type Handlers = { onPointerOver: (e: unknown) => void; onPointerOut: () => void };

let viewer: ViewerStore;
let root: ReconcilerRoot<HTMLCanvasElement>;
let scene: Scene;

beforeEach(async () => {
  viewer = createViewerStore('LIVE');
  viewer.getState().markReady();
  const canvas = document.createElement('canvas');
  root = createRoot(canvas);
  await root.configure({
    gl: fakeRenderer(canvas) as never,
    size: { width: 800, height: 600, top: 0, left: 0 },
    frameloop: 'never',
  });
  function Grab() {
    const s = useThree((st) => st.scene);
    useEffect(() => {
      scene = s;
    }, [s]);
    return null;
  }
  await act(async () =>
    root.render(
      <ViewerStoreContext value={viewer}>
        <Grab />
        <Selectable target={Z1}>
          <mesh />
        </Selectable>
        <Selectable target={Z2}>
          <mesh />
        </Selectable>
      </ViewerStoreContext>,
    ),
  );
  // 장면은 이미 올라와 있다. 이제부터 머무름 시간만 가짜 시계로 잰다.
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
});
afterEach(async () => {
  vi.useRealTimers();
  await act(async () => root.unmount());
});

const handlersOf = (name: string) =>
  (scene.getObjectByName(name) as unknown as { __r3f: { handlers: Handlers } }).__r3f.handlers;
const over = (name: string) => handlersOf(name).onPointerOver({ stopPropagation: () => {} });

describe('3D 이름표 머무름', () => {
  it('머무름 시간은 300ms 다', () => {
    expect(HOVER_LABEL_MS).toBe(300);
  });

  it('같은 대상에 300ms 머물면 이름표 대상이 되고, 그 전에는 아니다', () => {
    over('zone:z-01');
    vi.advanceTimersByTime(HOVER_LABEL_MS - 1);
    expect(viewer.getState().hovered).toBeNull();
    vi.advanceTimersByTime(1);
    expect(viewer.getState().hovered).toBe('zone:z-01');
  });

  it('300ms 전에 나가면 이름표를 보이지 않고, 보인 뒤 나가면 거둔다', () => {
    over('zone:z-01');
    vi.advanceTimersByTime(HOVER_LABEL_MS - 50);
    handlersOf('zone:z-01').onPointerOut();
    vi.advanceTimersByTime(HOVER_LABEL_MS);
    expect(viewer.getState().hovered).toBeNull();

    over('zone:z-01');
    vi.advanceTimersByTime(HOVER_LABEL_MS);
    expect(viewer.getState().hovered).toBe('zone:z-01');
    handlersOf('zone:z-01').onPointerOut();
    expect(viewer.getState().hovered).toBeNull();
  });

  it('다른 대상으로 옮기면 그 대상에서 다시 300ms 를 센다', () => {
    over('zone:z-01');
    vi.advanceTimersByTime(HOVER_LABEL_MS);
    expect(viewer.getState().hovered).toBe('zone:z-01');

    handlersOf('zone:z-01').onPointerOut();
    over('zone:z-02');
    vi.advanceTimersByTime(HOVER_LABEL_MS - 1);
    expect(viewer.getState().hovered).toBeNull();
    vi.advanceTimersByTime(1);
    expect(viewer.getState().hovered).toBe('zone:z-02');
  });
});
