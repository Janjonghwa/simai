import type { ViewerStatus, ViewerTarget } from '@/features/viewer3d/types';
import { WORLD_REFERENCE_POINTS } from '@/features/viewer3d/WorldReference';
import { useThree, type RootState } from '@react-three/fiber';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Box3, Vector3, type Object3D } from 'three';

// Spatial 진단 도구. 같은 Canvas·renderer·Scene·Camera 가 유지되는지, World 기준점이 어디 놓였는지 숫자로 확인한다.
// 측정은 버튼을 누를 때만 한다. 접혀 있거나 다른 보기일 때는 아무것도 읽지 않는다.
const ids = new WeakMap<object, number>();
let nextId = 1;
const idOf = (value: object) => {
  if (!ids.has(value)) ids.set(value, nextId++);
  return ids.get(value)!;
};
const renderers = new Set<number>();
let readState: (() => RootState) | null = null;

/** Canvas 안에 두어 렌더러 상태를 읽을 수 있게 한다. 화면에는 아무것도 그리지 않아 모든 보기에 둔다. */
export function DebugProbe() {
  const get = useThree((s) => s.get);
  useEffect(() => {
    readState = get;
    renderers.add(idOf(get().gl));
  }, [get]);
  return null;
}

const round = (n: number) => Math.round(n * 1000) / 1000;

// 기준점이 장면에 실제로 놓인 Three 좌표. World 기준점을 띄운 보기에서만 값이 있다.
function worldRefsOf(state: RootState) {
  const out: Record<string, number[] | null> = {};
  for (const point of WORLD_REFERENCE_POINTS) {
    const object = state.scene.getObjectByName(`world-ref:${point.key}`);
    out[point.key] = object ? object.getWorldPosition(new Vector3()).toArray().map(round) : null;
  }
  return out;
}

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
      'world-ref:O': screenOf(state, state.scene.getObjectByName('world-ref:O')),
    },
    worldRefs: worldRefsOf(state),
  };
}

/** World 기준 표시 범례. DEMO 교실이 실측이 아님을 함께 알린다. */
function WorldLegend() {
  return (
    <div className="smk-legend" data-testid="world-legend">
      <p>원점 O = 교실 모서리 바닥(Z=0)</p>
      <p>
        <b className="smk-axis" data-axis="X">
          X
        </b>{' '}
        <b className="smk-axis" data-axis="Y">
          Y
        </b>{' '}
        <b className="smk-axis" data-axis="Z">
          Z(위)
        </b>{' '}
        · 표식 간격 1m
      </p>
      <p className="smk-hint">DEMO 10×10m 교실 · 실측·캘리브레이션 아님</p>
    </div>
  );
}

interface SpatialDiagnosticsProps {
  open: boolean;
  onToggle: () => void;
  status: ViewerStatus | null;
  selected: ViewerTarget | null;
}

/** World 범례와 측정을 한 곳에 모은 접이식 진단 영역. 접으면 제목과 뷰어 상태 한 줄만 남는다. */
export function SpatialDiagnostics({ open, onToggle, status, selected }: SpatialDiagnosticsProps) {
  const [out, setOut] = useState('');
  return (
    <section
      aria-label="Spatial 진단"
      data-testid="spatial-diagnostics"
      className="smk-diag smk-float"
    >
      <button
        type="button"
        aria-expanded={open}
        aria-controls="smk-diag-body"
        className="smk-diag-head"
        onClick={onToggle}
      >
        <span className="smk-card-title">Spatial 진단</span>
        <span className="smk-diag-state smk-mono" data-testid="diag-status">
          {status ? `${status.phase} · ${status.mode}` : '—'}
        </span>
        {open ? <ChevronDown aria-hidden /> : <ChevronUp aria-hidden />}
      </button>
      {open && (
        <div id="smk-diag-body" className="smk-diag-body">
          <WorldLegend />
          <p className="smk-id smk-mono" data-testid="diag-selected">
            선택 {selected ? `${selected.type}:${selected.id}` : '없음'}
          </p>
          <button
            type="button"
            className="smk-chip"
            onClick={() => setOut(JSON.stringify(measure()))}
          >
            측정
          </button>
          <pre data-testid="debug-out" className="smk-mono">
            {out}
          </pre>
        </div>
      )}
    </section>
  );
}
