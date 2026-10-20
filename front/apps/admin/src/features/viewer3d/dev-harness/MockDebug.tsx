import { useThree, type RootState } from '@react-three/fiber';
import { useEffect, useState } from 'react';
import { Box3, Vector3, type Object3D } from 'three';

// ?debug=1 일 때만 쓰는 측정 도구. 같은 Canvas·renderer·Scene·Camera 가 유지되는지 숫자로 확인한다.
const ids = new WeakMap<object, number>();
let nextId = 1;
const idOf = (value: object) => {
  if (!ids.has(value)) ids.set(value, nextId++);
  return ids.get(value)!;
};
const renderers = new Set<number>();
let readState: (() => RootState) | null = null;

export function readDebugFlag(search: string): boolean {
  return new URLSearchParams(search).get('debug') === '1';
}

/** Canvas 안에 두어 렌더러 상태를 읽을 수 있게 한다. */
export function DebugProbe() {
  const get = useThree((s) => s.get);
  useEffect(() => {
    readState = get;
    renderers.add(idOf(get().gl));
  }, [get]);
  return null;
}

const round = (n: number) => Math.round(n * 1000) / 1000;

function screenOf(state: RootState, object: Object3D | undefined) {
  if (!object) return null;
  const point = new Box3().setFromObject(object).getCenter(new Vector3()).project(state.camera);
  const rect = state.gl.domElement.getBoundingClientRect();
  return [
    Math.round(rect.left + ((point.x + 1) / 2) * rect.width),
    Math.round(rect.top + ((1 - point.y) / 2) * rect.height),
  ];
}

function measure() {
  const canvas = document.querySelector('canvas');
  const state = readState?.();
  if (!state || !canvas) return { canvases: document.querySelectorAll('canvas').length };
  const controls = state.controls as unknown as { target: Vector3 } | null;
  const target = controls?.target ?? new Vector3();
  return {
    canvases: document.querySelectorAll('canvas').length,
    canvasId: idOf(canvas),
    rendererId: idOf(state.gl),
    sceneId: idOf(state.scene),
    cameraId: idOf(state.camera),
    renderersCreated: renderers.size,
    camera: state.camera.position.toArray().map(round),
    target: target.toArray().map(round),
    distance: round(state.camera.position.distanceTo(target)),
    size: [state.size.width, state.size.height],
    screen: {
      'person:worker1': screenOf(state, state.scene.getObjectByName('person:worker1')),
      'zone:zoneA': screenOf(state, state.scene.getObjectByName('zone:zoneA')),
    },
  };
}

export function DebugPanel() {
  const [out, setOut] = useState('');
  return (
    <div className="smk-debug smk-float smk-mono">
      <button type="button" className="smk-chip" onClick={() => setOut(JSON.stringify(measure()))}>
        측정
      </button>
      <pre data-testid="debug-out">{out}</pre>
    </div>
  );
}
