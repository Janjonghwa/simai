import type { SpatialDataStore } from '@/features/viewer3d/data/spatialData.store';
import type {
  ViewerEditBox,
  ViewerEditPreview,
  ViewerEditResult,
} from '@/features/viewer3d/editing/editTypes';
import { VIEWER_MODES, type Viewer3DHandle, type ViewerTarget } from '@/features/viewer3d/types';
import { useThree } from '@react-three/fiber';
import { useEffect, type RefObject } from 'react';
import { Vector3 } from 'three';
import { useStore } from 'zustand';

// 개발 화면 전용 편집 소비 예시(S15P31A101-195). FE 가 Viewer3D 의 beginEdit·cancelEdit·finishEdit 와
// onEditPreview·onEditResult 를 어떻게 받는지 보여 준다. 저장·적용·서버 호출은 흉내 내지 않는다(받은 World 값만 보인다).

const r3 = (v: number) => Math.round(v * 1000) / 1000;
const fmt = (box: ViewerEditBox) =>
  `min [${box.min.map(r3).join(', ')}] · max [${box.max.map(r3).join(', ')}]`;

/** 전후 차이(m). 0 이 아닌 값만 적는다. */
function diff(before: ViewerEditBox, after: ViewerEditBox) {
  const parts: string[] = [];
  (['min', 'max'] as const).forEach((side) =>
    ['x', 'y', 'z'].forEach((axis, i) => {
      const d = r3(after[side][i]! - before[side][i]!);
      if (d !== 0) parts.push(`${side}.${axis} ${d > 0 ? '+' : ''}${d}`);
    }),
  );
  return parts.length ? parts.join(' · ') : '변화 없음';
}

interface EditPanelProps {
  viewer: RefObject<Viewer3DHandle | null>;
  store: SpatialDataStore;
  selected: ViewerTarget | null;
  preview: ViewerEditPreview | null;
  result: ViewerEditResult | null;
}

export function EditPanel({ viewer, store, selected, preview, result }: EditPanelProps) {
  const zones = useStore(store, (s) => s.zones.zones);
  return (
    <section className="smk-data" aria-label="3D 편집(DEV)" data-testid="edit-panel">
      <p className="smk-card-title">3D 편집 · DEV 예시(저장 안 함)</p>
      <div className="smk-data-row">
        {zones.map((zone) => (
          <button
            key={zone.zoneId}
            type="button"
            className="smk-chip"
            onClick={() => viewer.current?.beginEdit({ type: 'zone', id: zone.zoneId })}
          >
            {zone.zoneId} 편집
          </button>
        ))}
        <button
          type="button"
          className="smk-chip"
          disabled={!selected}
          onClick={() => selected && viewer.current?.beginEdit(selected)}
        >
          선택 대상 편집
        </button>
        <button type="button" className="smk-chip" onClick={() => viewer.current?.cancelEdit()}>
          취소
        </button>
        <button type="button" className="smk-chip" onClick={() => viewer.current?.finishEdit()}>
          완료(결과 받기)
        </button>
      </div>
      <div className="smk-data-row">
        {VIEWER_MODES.map((mode) => (
          <button
            key={mode}
            type="button"
            className="smk-chip"
            onClick={() => viewer.current?.setMode(mode)}
          >
            모드 {mode}
          </button>
        ))}
      </div>
      <dl className="smk-data-grid smk-mono">
        <dt>초안</dt>
        <dd data-testid="edit-preview" data-editing={preview ? '' : undefined}>
          {preview
            ? `${preview.target.id} · ${preview.mode} · ${fmt(preview.draft)} · ${diff(preview.before, preview.draft)}`
            : '없음'}
        </dd>
        <dt>결과</dt>
        <dd data-testid="edit-result">
          {!result
            ? '없음'
            : result.status === 'FINISHED'
              ? `FINISHED · ${result.target.id} · 전 ${fmt(result.before)} → 후 ${fmt(result.after)} · ${diff(result.before, result.after)} · saved=false`
              : result.status === 'CANCELLED'
                ? `CANCELLED(${result.reason}) · ${result.target.id} · 원본 ${fmt(result.before)}`
                : `REJECTED(${result.reason}) · ${result.detail}`}
        </dd>
      </dl>
      {result && (
        <pre className="smk-mono" data-testid="edit-result-json">
          {JSON.stringify(result)}
        </pre>
      )}
    </section>
  );
}

/**
 * Canvas 안에 두어 손잡이의 화면 좌표와 렌더러 자원 수를 canvas 의 data-edit-probe 에 적는다(자동 브라우저 검증용, 개발 화면만).
 */
export function EditProbe() {
  const get = useThree((s) => s.get);
  useEffect(() => {
    const timer = setInterval(() => {
      const { scene, camera, gl, controls } = get();
      const rect = gl.domElement.getBoundingClientRect();
      const handles: Record<string, [number, number]> = {};
      const marks: Record<string, number[]> = {};
      scene.traverse((node) => {
        if (node.name.startsWith('risk-zone:') || node.name.startsWith('zone-box:')) {
          marks[node.name] = node
            .getWorldPosition(new Vector3())
            .toArray()
            .map((v) => Math.round(v * 1000) / 1000);
        }
        if (!node.name.startsWith('edit-handle:')) return;
        const p = node.getWorldPosition(new Vector3()).project(camera);
        handles[node.name.slice(12)] = [
          Math.round(rect.left + ((p.x + 1) / 2) * rect.width),
          Math.round(rect.top + ((1 - p.y) / 2) * rect.height),
        ];
      });
      gl.domElement.dataset.editProbe = JSON.stringify({
        handles,
        marks,
        orbitEnabled: (controls as { enabled?: boolean } | null)?.enabled ?? null,
        camera: camera.position.toArray().map((v) => Math.round(v * 1000) / 1000),
        textures: gl.info.memory.textures,
        geometries: gl.info.memory.geometries,
        programs: gl.info.programs?.length ?? null,
        frame: gl.info.render.frame,
      });
    }, 250);
    return () => clearInterval(timer);
  }, [get]);
  return null;
}
