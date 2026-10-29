import { useSpatialData } from '@/features/viewer3d/data/SpatialDataProvider';
import {
  IDLE_LOAD,
  readCurrentScene,
  sceneStatusOf,
  type CurrentScene,
  type SceneDisplay,
} from '@/features/viewer3d/scene/sceneContract';
import {
  SceneLoaderContext,
  SceneReviewContext,
  SceneStoreContext,
  type SceneStore,
} from '@/features/viewer3d/scene/sceneStore';
import { threeToWorld, toThreePoint } from '@/features/viewer3d/worldCoordinates';
import { useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Box3, Mesh, type Material, type Object3D, type Texture } from 'three';
import { useStore } from 'zustand';

// 정적 배경 Scene 레이어(S15P31A101-59). 같은 Canvas·깊이 버퍼에 그려 책상 뒤 사람의 가림이 맞게 한다(명세 13 머리말).
// - 받은 glTF 는 GLTF_Y_UP 계약대로 이미 Three 축이라 회전·이동·배율을 더하지 않는다(축 변환 한 곳 원칙).
// - 배경은 클릭을 받지 않는다(사람·구역·작업면 선택과 빈 곳 클릭 해제를 가리지 않는다).
// - 정합 근거가 없으면(시연용 기본·버전 비교 전) 흐리게, 버전이 어긋나면 숨기고 기본 바닥을 보인다.
// - 늦게 끝난 이전 Scene 읽기는 버리고 자원을 해제한다. 바뀌거나 내려가면 이전 자산을 해제한다.

const noRaycast = () => {};
/** 정합 근거가 없을 때 배경 불투명도. 사람·구역보다 눈에 덜 띄게 한다. */
export const DIMMED_OPACITY = 0.35;

/** 장면 아래 모든 형상·재질·텍스처를 해제한다. */
export function disposeObject(root: Object3D) {
  root.traverse((node) => {
    if (!(node instanceof Mesh)) return;
    node.geometry?.dispose();
    const materials: Material[] = Array.isArray(node.material) ? node.material : [node.material];
    for (const material of materials) {
      if (!material) continue;
      for (const value of Object.values(material)) {
        if (value && typeof value === 'object' && (value as Texture).isTexture) {
          (value as Texture).dispose();
        }
      }
      material.dispose();
    }
  });
}

/** 자산의 World 경계(m). 비었거나 유한하지 않으면 null(그리지 않는다). */
export function worldBoundsOf(root: Object3D) {
  root.updateWorldMatrix(true, true);
  const box = new Box3().setFromObject(root);
  if (box.isEmpty()) return null;
  const a = toThreePoint(box.min.toArray());
  const b = toThreePoint(box.max.toArray());
  if (!a || !b) return null;
  const [p, q] = [threeToWorld(a), threeToWorld(b)];
  // Three Z 가 World −Y 라 축마다 작은 값·큰 값을 다시 고른다.
  const min = [0, 1, 2].map((i) => Math.min(p[i]!, q[i]!)) as [number, number, number];
  const max = [0, 1, 2].map((i) => Math.max(p[i]!, q[i]!)) as [number, number, number];
  return { min, max };
}

/** 배경으로 쓰기 전 준비: 클릭을 받지 않게 하고, 흐리게 바꿀 수 있게 재질을 이 장면 전용으로 복사해 원래 값을 기억한다. */
function prepare(root: Object3D) {
  root.traverse((node) => {
    if (!(node instanceof Mesh)) return;
    node.raycast = noRaycast;
    const copy = (m: Material) => {
      const c = m.clone();
      c.userData.sceneBase = { transparent: c.transparent, opacity: c.opacity };
      m.dispose();
      return c;
    };
    node.material = Array.isArray(node.material) ? node.material.map(copy) : copy(node.material);
  });
}

function applyDisplay(root: Object3D, display: SceneDisplay) {
  root.visible = display !== 'HIDDEN';
  root.traverse((node) => {
    if (!(node instanceof Mesh)) return;
    const materials: Material[] = Array.isArray(node.material) ? node.material : [node.material];
    for (const m of materials) {
      const base = m.userData.sceneBase as { transparent: boolean; opacity: number } | undefined;
      if (!base) continue;
      const dim = display === 'DIMMED';
      m.transparent = dim || base.transparent;
      m.opacity = dim ? Math.min(base.opacity, DIMMED_OPACITY) : base.opacity;
      m.needsUpdate = true;
    }
  });
}

/** HTTP 상태가 실린 읽기 오류면 그 값(three FileLoader 의 HttpError.response.status). 주소는 꺼내지 않는다. */
function httpStatusOf(error: unknown): number | null {
  const status = (error as { response?: { status?: unknown } } | null)?.response?.status;
  return typeof status === 'number' ? status : null;
}

export function SceneBackground() {
  const store = useContext(SceneStoreContext);
  return store ? <Background store={store} /> : null;
}

function Background({ store }: { store: SceneStore }) {
  const loader = useContext(SceneLoaderContext);
  const review = useContext(SceneReviewContext);
  const input = useStore(store, (s) => s.input);
  const load = useStore(store, (s) => s.load);
  const worldScene = useSpatialData((s) => s.world?.sceneVersion ?? null);
  const worldCalibration = useSpatialData((s) => s.world?.calibrationVersion ?? null);
  const healthCommitted = useSpatialData((s) => s.health?.scene.committedVersion ?? null);
  const healthLive = useSpatialData((s) => s.health?.scene.liveVersion ?? null);
  const healthMismatch = useSpatialData((s) => (s.health ? s.health.scene.mismatch : null));
  const scene: CurrentScene | null = useMemo(() => {
    if (!input?.ok) return null;
    const read = readCurrentScene(input.raw);
    return read.ok ? read.scene : null;
  }, [input]);
  const [loaded, setLoaded] = useState<{ sceneVersion: string; root: Object3D } | null>(null);
  const loadedRef = useRef(loaded);
  useEffect(() => {
    loadedRef.current = loaded;
  }, [loaded]);

  const sceneVersion = scene?.sceneVersion ?? null;
  const url = scene?.gltfUrl ?? null;
  const expiresAt = scene?.gltfExpiresAtMs ?? null;

  useEffect(() => {
    // 같은 Scene 을 이미 읽었으면 서명 주소만 새로 받은 것이라 다시 읽지 않는다.
    if (sceneVersion && loadedRef.current?.sceneVersion === sceneVersion) return;
    setLoaded(null);
    if (!sceneVersion || !url) {
      store.getState().setLoad(IDLE_LOAD);
      return;
    }
    const at = (phase: 'LOADING' | 'FAILED' | 'EXPIRED', reason: string | null = null) =>
      store.getState().setLoad({ phase, sceneVersion, reason, boundsWorld: null });
    if (expiresAt !== null && expiresAt <= Date.now()) {
      at('EXPIRED', '서명 주소가 만료됐다');
      return;
    }
    let alive = true;
    at('LOADING');
    loader(url).then(
      (root) => {
        if (!alive) {
          disposeObject(root);
          return;
        }
        const boundsWorld = worldBoundsOf(root);
        if (!boundsWorld) {
          disposeObject(root);
          at('FAILED', '빈 자산이다');
          return;
        }
        prepare(root);
        root.name = `scene-background:${sceneVersion}`;
        setLoaded({ sceneVersion, root });
        store.getState().setLoad({ phase: 'LOADED', sceneVersion, reason: null, boundsWorld });
      },
      (error: unknown) => {
        if (!alive) return;
        const status = httpStatusOf(error);
        const expired =
          status === 401 || status === 403 || (expiresAt !== null && expiresAt <= Date.now());
        if (expired) at('EXPIRED', status ? `HTTP ${status}` : '서명 주소가 만료됐다');
        else at('FAILED', status ? `HTTP ${status}` : '자산을 읽지 못했다(형식 오류 등)');
      },
    );
    return () => {
      alive = false;
    };
  }, [store, loader, sceneVersion, url, expiresAt]);

  // 바뀌거나 내려가면 이전 자산을 해제한다.
  useEffect(() => {
    if (!loaded) return;
    return () => disposeObject(loaded.root);
  }, [loaded]);

  const display = sceneStatusOf(input, load, {
    worldScene,
    worldCalibration,
    healthCommitted,
    healthLive,
    healthMismatch,
  }).display;
  const shown: SceneDisplay = loaded && loaded.sceneVersion === sceneVersion ? display : 'HIDDEN';

  useEffect(() => {
    // 검수 표시는 흐림만 거둔다. 숨김(HIDDEN)·판정은 그대로다.
    if (loaded) applyDisplay(loaded.root, review && shown === 'DIMMED' ? 'FULL' : shown);
    store.getState().setDisplay(shown);
  }, [store, loaded, shown, review]);
  useEffect(() => () => store.getState().setDisplay('HIDDEN'), [store]);

  if (!loaded) return null;
  return (
    <group name="scene-background-layer">
      <primitive object={loaded.root} />
    </group>
  );
}
