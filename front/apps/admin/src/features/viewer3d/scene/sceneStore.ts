import type { SpatialDataState } from '@/features/viewer3d/data/spatialData.store';
import {
  IDLE_LOAD,
  type SceneDisplay,
  type SceneLoad,
  type SceneVersions,
  type ViewerSceneInput,
} from '@/features/viewer3d/scene/sceneContract';
import { createContext } from 'react';
import type { Object3D } from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { createStore, type StoreApi } from 'zustand/vanilla';

// 뷰어 인스턴스마다 하나 두는 배경 Scene 상태. 입력(FE 조회 결과)과 읽기 단계, 장면에 보이는 방식만 둔다.

export interface SceneStoreState {
  input: ViewerSceneInput | null;
  load: SceneLoad;
  /** 배경이 장면에 어떻게 보이는지. 기본 바닥은 HIDDEN 일 때만 채운다. */
  display: SceneDisplay;
  setInput(input: ViewerSceneInput | null): void;
  setLoad(load: SceneLoad): void;
  setDisplay(display: SceneDisplay): void;
}

export type SceneStore = StoreApi<SceneStoreState>;

export function createSceneStore(): SceneStore {
  return createStore<SceneStoreState>()((set, get) => ({
    input: null,
    load: IDLE_LOAD,
    display: 'HIDDEN',
    setInput: (input) => {
      if (get().input !== input) set({ input });
    },
    setLoad: (load) => set({ load }),
    setDisplay: (display) => {
      if (get().display !== display) set({ display });
    },
  }));
}

/** ViewerShell 밖(시험 등)이면 null 이고 배경을 그리지 않는다. 기본 바닥은 늘 보인다. */
export const SceneStoreContext = createContext<SceneStore | null>(null);

/** 배경 glTF 를 읽어 장면 루트를 돌려준다. 시험은 이 함수를 바꿔 끼운다. */
export type SceneAssetLoader = (url: string) => Promise<Object3D>;

export const loadGltfScene: SceneAssetLoader = (url) =>
  new GLTFLoader().loadAsync(url).then((gltf) => gltf.scene);

export const SceneLoaderContext = createContext<SceneAssetLoader>(loadGltfScene);

/** 정합 판정에 쓰는 지금 버전. World·Health 가 없으면 null. */
export const sceneVersionsOf = (state: SpatialDataState): SceneVersions => ({
  worldScene: state.world?.sceneVersion ?? null,
  worldCalibration: state.world?.calibrationVersion ?? null,
  healthCommitted: state.health?.scene.committedVersion ?? null,
  healthLive: state.health?.scene.liveVersion ?? null,
  healthMismatch: state.health ? state.health.scene.mismatch : null,
});
