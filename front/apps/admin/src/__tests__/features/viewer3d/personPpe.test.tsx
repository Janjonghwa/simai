import {
  createSpatialDataStore,
  type SpatialDataStore,
} from '@/features/viewer3d/data/spatialData.store';
import { SpatialDataContext } from '@/features/viewer3d/data/SpatialDataProvider';
import {
  crowdTrackId,
  scenarioTick,
  type ScenarioKey,
} from '@/features/viewer3d/dev-harness/scenarios';
import { PersonLayer } from '@/features/viewer3d/people/PersonLayer';
import { PpeAssetsContext, type PpeAssets } from '@/features/viewer3d/people/ppe/ppeAssets';
import { CAPSULE_HELMET, CAPSULE_VEST } from '@/features/viewer3d/people/ppe/ppeFit';
import type { PersonHelmetState } from '@/features/viewer3d/types';
import { createViewerStore, type ViewerStore } from '@/features/viewer3d/viewer3d.store';
import { ViewerStoreContext } from '@/features/viewer3d/ViewerStoreContext';
import { advance, createRoot, extend, useThree, type ReconcilerRoot } from '@react-three/fiber';
import { act, useEffect, type ReactNode } from 'react';
import type { Mesh, MeshStandardMaterial, Object3D, Scene } from 'three';
import * as THREE from 'three';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 골격 막대·캡슐 사람에게 붙는 안전모·조끼와 미착용 경고 표식(-552, -531). 가짜 renderer 로 실제 PersonLayer 의 장면 트리를 본다
// (personSkeleton.test.tsx 와 같은 방식). 모델은 단위 상자로 바꿔 넣는다(jsdom 은 GLB 텍스처를 풀지 못한다).
// 픽셀·모양은 개발 화면(?ppe=fixed&avatarMesh=off)에서 눈으로 본다.
extend(THREE as unknown as Parameters<typeof extend>[0]);
Reflect.set(globalThis, 'IS_REACT_ACT_ENVIRONMENT', true);

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

const T0 = 1_760_342_400_000;
let scene: Scene | null = null;
function SceneProbe() {
  const current = useThree((s) => s.scene);
  useEffect(() => {
    scene = current;
  }, [current]);
  return null;
}

const unitBox = () => new THREE.BoxGeometry(1, 1, 1);
const testAssets: PpeAssets = {
  helmet: { geometry: unitBox(), map: null },
  vest: { geometry: unitBox(), map: null },
};

let root: ReconcilerRoot<HTMLCanvasElement> | null = null;
let viewer: ViewerStore;
let data: SpatialDataStore;

/** 장비 모델을 바꿔 넣고 PersonLayer 를 그린다. assets 가 null 이면 읽지 못한 경우다. */
async function mount(assets: PpeAssets | null = testAssets) {
  const canvas = document.createElement('canvas');
  root ??= createRoot(canvas);
  await root.configure({
    gl: fakeRenderer(canvas) as never,
    size: { width: 800, height: 600, top: 0, left: 0 },
    frameloop: 'never',
  });
  const tree: ReactNode = (
    <ViewerStoreContext value={viewer}>
      <SpatialDataContext value={data}>
        <PpeAssetsContext value={assets}>
          <SceneProbe />
          <PersonLayer />
        </PpeAssetsContext>
      </SpatialDataContext>
    </ViewerStoreContext>
  );
  await act(async () => root!.render(tree));
}

async function frames(n = 3) {
  for (let i = 0; i < n; i++) await act(async () => advance(performance.now() + i * 16));
}

async function play(tick: number, scenario: ScenarioKey = 'normal') {
  await act(async () => {
    for (const out of scenarioTick(scenario, tick, T0)) {
      if (out.type === 'message') data.getState().ingest(out.raw, Date.now());
    }
  });
  await frames();
}

// normal 시나리오: w-0001 은 골격이 있어 막대로, w-0003 은 골격이 없어 캡슐로 그려진다(둘 다 관측 중).
// observation-loss 시나리오의 w-0002 는 tick 20~29 에 예측 상태로 접지점을 유지해 투명한 캡슐이 된다.
const SKELETON_ID = 'w-0001';
const CAPSULE_ID = 'w-0003';
const PREDICTED_CAPSULE_ID = 'w-0002';
const PREDICTED_TICK = 25;
/** crowd 시나리오의 높이 모르는 사람(예측·접지 모름 → 기둥). */
const COLUMN_ID = crowdTrackId(10);

const avatar = (id: string) => scene!.getObjectByName('person-avatar:' + id)!;
const gear = (id: string, kind: 'helmet' | 'vest') => avatar(id).getObjectByName(`ppe-${kind}`);
const marks = (id: string) => {
  const found: Object3D[] = [];
  avatar(id).traverse((o) => {
    if (o.name === 'ppe-warning-mark') found.push(o);
  });
  return found;
};

type States = Record<string, PersonHelmetState>;
/** 호스트가 따져 넘기는 안전모·조끼 상태(personHelmets·personVests)를 뷰어에 넣는다. */
async function setPpe(helmets: States | undefined, vests: States | undefined = helmets) {
  await act(async () => {
    viewer.getState().setPersonHelmets(helmets);
    viewer.getState().setPersonVests(vests);
  });
  await frames();
}
const both = (id: string, helmet: PersonHelmetState, vest: PersonHelmetState) =>
  [{ [id]: helmet }, { [id]: vest }] as const;

const meshesOf = (node: Object3D) => {
  const found: Mesh[] = [];
  node.traverse((o) => {
    if ((o as Mesh).isMesh) found.push(o as Mesh);
  });
  return found;
};
const hasLines = (node: Object3D) => {
  let found = false;
  node.traverse((o) => {
    if ((o as THREE.LineSegments).isLineSegments) found = true;
  });
  return found;
};

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
  viewer = createViewerStore();
  viewer.getState().markReady();
  data = createSpatialDataStore();
  data.getState().reset('mock:crowd');
  data.getState().setConnection({ state: 'OPEN', at: Date.now() });
});

afterEach(async () => {
  await act(async () => root?.unmount());
  root = null;
  scene = null;
  vi.restoreAllMocks();
});

describe('PersonLayer 안전모·조끼(-552)', () => {
  it('상태가 없으면 장비도 경고 표식도 붙이지 않는다', async () => {
    await mount();
    await play(3);
    expect(gear(SKELETON_ID, 'helmet')).toBeUndefined();
    expect(gear(CAPSULE_ID, 'vest')).toBeUndefined();
    expect(marks(SKELETON_ID)).toHaveLength(0);
    expect(marks(CAPSULE_ID)).toHaveLength(0);
  });

  it('골격 막대 사람: 착용은 불투명 실물로 골격 그룹 안에 붙고 머리·어깨 높이에 놓인다', async () => {
    await mount();
    await play(3);
    await setPpe(...both(SKELETON_ID, 'WORN', 'WORN'));

    const helmet = gear(SKELETON_ID, 'helmet')!;
    const vest = gear(SKELETON_ID, 'vest')!;
    expect(helmet.parent?.name).toBe('person-skeleton-root');
    expect(helmet.visible).toBe(true);
    const worn = meshesOf(helmet);
    expect(worn).toHaveLength(1);
    expect((worn[0]!.material as MeshStandardMaterial).transparent).toBe(false);
    expect(hasLines(helmet)).toBe(false);
    // 머리 높이(귀 높이 근처)에 있다.
    expect(helmet.position.y).toBeGreaterThan(1.3);
    expect(helmet.position.y).toBeLessThan(1.9);

    expect(vest.visible).toBe(true);
    expect(hasLines(vest)).toBe(false);
    // 조끼는 어깨 높이 아래로 내려온다.
    expect(vest.position.y).toBeGreaterThan(1.0);
    expect(vest.scale.y).toBeGreaterThan(0.4);
    expect(marks(SKELETON_ID)).toHaveLength(0);
  });

  it('모름(UNKNOWN)은 장비를 그리지 않는다(반투명 장비가 착용과 구분되지 않았다, -656)', async () => {
    await mount();
    await play(3);
    await setPpe(...both(SKELETON_ID, 'WORN', 'UNKNOWN'));
    expect(gear(SKELETON_ID, 'helmet')).toBeDefined();
    expect(gear(SKELETON_ID, 'vest')).toBeUndefined();

    await setPpe(...both(CAPSULE_ID, 'UNKNOWN', 'UNKNOWN'));
    expect(gear(CAPSULE_ID, 'helmet')).toBeUndefined();
    expect(gear(CAPSULE_ID, 'vest')).toBeUndefined();
    expect(marks(CAPSULE_ID)).toHaveLength(0);
  });

  it('캡슐 사람: 몸 그룹 안에 고정 자리로 붙는다', async () => {
    await mount();
    await play(3);
    await setPpe(...both(CAPSULE_ID, 'WORN', 'WORN'));

    const helmet = gear(CAPSULE_ID, 'helmet')!;
    const vest = gear(CAPSULE_ID, 'vest')!;
    expect(helmet.parent?.name).not.toBe('person-skeleton-root');
    expect(helmet.position.toArray()).toEqual([...CAPSULE_HELMET.position]);
    expect(vest.position.toArray()).toEqual([...CAPSULE_VEST.position]);
    expect(helmet.scale.toArray().map((v) => Number(v.toFixed(3)))).toEqual([
      ...CAPSULE_HELMET.size,
    ]);
    expect((meshesOf(helmet)[0]!.material as MeshStandardMaterial).opacity).toBe(1);
  });

  it('예측 상태라 흐려진 캡슐 사람에게는 장비도 같은 불투명도로 붙는다', async () => {
    await mount();
    await play(PREDICTED_TICK, 'observation-loss');
    await setPpe(...both(PREDICTED_CAPSULE_ID, 'WORN', 'WORN'));
    const helmet = gear(PREDICTED_CAPSULE_ID, 'helmet')!;
    expect(helmet).toBeDefined();
    expect((meshesOf(helmet)[0]!.material as MeshStandardMaterial).opacity).toBe(0.45);
  });

  it('착용 → 모름이면 그 장비가 사라지고, 다시 착용이면 붙고, 비우면 모두 사라진다', async () => {
    await mount();
    await play(3);
    await setPpe(...both(SKELETON_ID, 'WORN', 'WORN'));
    expect(gear(SKELETON_ID, 'helmet')).toBeDefined();
    await setPpe(...both(SKELETON_ID, 'UNKNOWN', 'WORN'));
    expect(gear(SKELETON_ID, 'helmet')).toBeUndefined();
    expect(gear(SKELETON_ID, 'vest')).toBeDefined();
    await setPpe(...both(SKELETON_ID, 'WORN', 'WORN'));
    expect(gear(SKELETON_ID, 'helmet')).toBeDefined();
    await setPpe(undefined);
    expect(gear(SKELETON_ID, 'helmet')).toBeUndefined();
    expect(gear(SKELETON_ID, 'vest')).toBeUndefined();
  });

  it('장비와 경고 표식은 클릭·초점에 끼지 않는다(raycast 없음)', async () => {
    await mount();
    await play(3);
    await setPpe(
      { [SKELETON_ID]: 'WORN', [CAPSULE_ID]: 'NOT_WORN' },
      { [SKELETON_ID]: 'WORN', [CAPSULE_ID]: 'WORN' },
    );
    const pieces = [
      ...meshesOf(gear(SKELETON_ID, 'helmet')!),
      ...meshesOf(gear(SKELETON_ID, 'vest')!),
      ...meshesOf(gear(CAPSULE_ID, 'vest')!),
      ...marks(CAPSULE_ID).flatMap(meshesOf),
    ];
    expect(pieces.length).toBeGreaterThan(3);
    for (const piece of pieces) {
      const hits: unknown[] = [];
      piece.raycast({} as never, hits as never);
      expect(hits).toHaveLength(0);
    }
    // 사람 선택 대상은 그대로다.
    expect(viewer.getState().targets.has('person:' + SKELETON_ID)).toBe(true);
  });

  it('장비를 읽지 못해도(null) 사람은 그리고 경고 표식은 보인다', async () => {
    await mount(null);
    await play(3);
    await setPpe(...both(SKELETON_ID, 'WORN', 'NOT_WORN'));
    expect(avatar(SKELETON_ID)).toBeDefined();
    expect(gear(SKELETON_ID, 'helmet')).toBeUndefined();
    expect(marks(SKELETON_ID)).toHaveLength(1);
  });
});

describe('PersonLayer 미착용 경고 표식(-531·-552)', () => {
  it('안전모·조끼 중 하나라도 미착용이면 표식 하나가 보이고, 미착용인 장비만 그리지 않는다', async () => {
    await mount();
    await play(3);
    await setPpe(...both(SKELETON_ID, 'NOT_WORN', 'WORN'));
    expect(gear(SKELETON_ID, 'helmet')).toBeUndefined();
    expect(gear(SKELETON_ID, 'vest')).toBeDefined();
    expect(marks(SKELETON_ID)).toHaveLength(1);

    await setPpe(...both(SKELETON_ID, 'WORN', 'NOT_WORN'));
    expect(gear(SKELETON_ID, 'helmet')).toBeDefined();
    expect(gear(SKELETON_ID, 'vest')).toBeUndefined();
    expect(marks(SKELETON_ID)).toHaveLength(1);
  });

  it('둘 다 미착용이어도 표식은 하나다', async () => {
    await mount();
    await play(3);
    await setPpe(...both(CAPSULE_ID, 'NOT_WORN', 'NOT_WORN'));
    expect(marks(CAPSULE_ID)).toHaveLength(1);
    expect(gear(CAPSULE_ID, 'helmet')).toBeUndefined();
    expect(gear(CAPSULE_ID, 'vest')).toBeUndefined();
  });

  it('모름·착용만이면 표식이 없다', async () => {
    await mount();
    await play(3);
    await setPpe(...both(CAPSULE_ID, 'UNKNOWN', 'WORN'));
    expect(marks(CAPSULE_ID)).toHaveLength(0);
  });

  it('골격 막대는 골격 머리 위를, 캡슐은 머리 구 위(1.64 m)를 따른다', async () => {
    await mount();
    await play(3);
    await setPpe(
      { [SKELETON_ID]: 'NOT_WORN', [CAPSULE_ID]: 'NOT_WORN' },
      { [SKELETON_ID]: 'WORN', [CAPSULE_ID]: 'WORN' },
    );
    const skeletonMark = marks(SKELETON_ID)[0]!;
    expect(skeletonMark.parent?.name).toBe('person-skeleton-root');
    expect(skeletonMark.position.y).toBeGreaterThan(1.5);
    expect(marks(CAPSULE_ID)[0]!.position.y).toBeCloseTo(1.64, 5);
  });

  it('높이 모르는 기둥 사람에게는 표식만 두고 장비는 그리지 않는다', async () => {
    await mount();
    await play(3, 'crowd');
    await setPpe(...both(COLUMN_ID, 'WORN', 'NOT_WORN'));
    expect(marks(COLUMN_ID)).toHaveLength(1);
    expect(marks(COLUMN_ID)[0]!.position.y).toBeCloseTo(2.0, 5);
    expect(gear(COLUMN_ID, 'helmet')).toBeUndefined();
    expect(gear(COLUMN_ID, 'vest')).toBeUndefined();
  });
});
