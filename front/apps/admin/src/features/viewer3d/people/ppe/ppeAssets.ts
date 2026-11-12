import helmetUrl from '@/features/viewer3d/people/ppe/assets/helmet.glb?url';
import vestUrl from '@/features/viewer3d/people/ppe/assets/vest.glb?url';
import { createContext, useContext, useEffect, useState } from 'react';
import {
  Mesh,
  MeshStandardMaterial,
  Vector3,
  type BufferGeometry,
  type Object3D,
  type Texture,
} from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';

// 안전모·조끼 모델(CC BY 4.0, assets/CREDITS.txt). 사람마다 새로 만들지 않고 형상·재질을 모두가 함께 쓴다.

/** 모양을 가로·세로·앞뒤 1 인 상자에 맞춰 둔 장비 하나. 실제 크기는 ppeFit 이 정한다. */
export interface GearAsset {
  /** 단위 상자 모양. 원점은 안전모 = 모자 아래 가운데, 조끼 = 위쪽 가운데. */
  geometry: BufferGeometry;
  /** 기본 색 텍스처(채도를 낮춰 구웠다). */
  map: Texture | null;
}

export interface PpeAssets {
  helmet: GearAsset;
  vest: GearAsset;
}

/** 안전모는 아래 가운데, 조끼는 위쪽 가운데를 원점으로 삼는다. */
export type GearPivot = 'BOTTOM' | 'TOP';

function firstMesh(scene: Object3D): Mesh | null {
  let found: Mesh | null = null;
  scene.traverse((object) => {
    if (!found && object instanceof Mesh) found = object;
  });
  return found;
}

/** glTF 장면의 첫 메시를 노드 변환을 구워 넣은 단위 상자 모양으로 만든다. 메시가 없으면 null. */
export function normalizeGear(scene: Object3D, pivot: GearPivot): GearAsset | null {
  scene.updateMatrixWorld(true);
  const mesh = firstMesh(scene);
  if (!mesh) return null;
  const geometry = mesh.geometry.clone().applyMatrix4(mesh.matrixWorld);
  geometry.computeBoundingBox();
  const box = geometry.boundingBox!;
  const size = box.getSize(new Vector3());
  const center = box.getCenter(new Vector3());
  const originY = pivot === 'BOTTOM' ? box.min.y : box.max.y;
  geometry.translate(-center.x, -originY, -center.z);
  geometry.scale(1 / size.x, 1 / size.y, 1 / size.z);
  geometry.computeBoundingSphere();
  const material = mesh.material;
  const map = material instanceof MeshStandardMaterial ? material.map : null;
  return { geometry, map };
}

/** 받아 온 GLB 두 개를 장비로 바꾼다. 읽지 못하거나 메시가 없으면 null. */
export async function parsePpeAssets(
  helmet: ArrayBuffer,
  vest: ArrayBuffer,
): Promise<PpeAssets | null> {
  const loader = new GLTFLoader();
  const [helmetGltf, vestGltf] = await Promise.all([
    loader.parseAsync(helmet, ''),
    loader.parseAsync(vest, ''),
  ]);
  const helmetAsset = normalizeGear(helmetGltf.scene, 'BOTTOM');
  const vestAsset = normalizeGear(vestGltf.scene, 'TOP');
  return helmetAsset && vestAsset ? { helmet: helmetAsset, vest: vestAsset } : null;
}

async function fetchBuffer(url: string): Promise<ArrayBuffer> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} ${response.status}`);
  return response.arrayBuffer();
}

let loading: Promise<PpeAssets | null> | null = null;

/** 안전모·조끼를 한 번만 읽는다. 실패하면 경고만 남기고 null(장비 없이 사람만 그린다). */
export function loadPpeAssets(): Promise<PpeAssets | null> {
  loading ??= Promise.all([fetchBuffer(helmetUrl), fetchBuffer(vestUrl)])
    .then(([helmet, vest]) => parsePpeAssets(helmet, vest))
    .catch((error: unknown) => {
      console.warn('안전모·조끼 모델을 읽지 못해 장비 없이 그린다', error);
      return null;
    });
  return loading;
}

/** 시험·개발 화면이 모델을 바꿔 넣는다. undefined 면 실제 파일을 읽는다. */
export const PpeAssetsContext = createContext<PpeAssets | null | undefined>(undefined);

/** 사람 레이어가 쓸 장비. 그릴 장비가 생겼을 때(wanted)부터 읽기 시작한다. 읽는 동안과 실패했을 때는 null. */
export function usePpeAssets(wanted: boolean): PpeAssets | null {
  const injected = useContext(PpeAssetsContext);
  const [loaded, setLoaded] = useState<PpeAssets | null>(null);
  useEffect(() => {
    if (injected !== undefined || !wanted) return;
    let alive = true;
    void loadPpeAssets().then((assets) => {
      if (alive) setLoaded(assets);
    });
    return () => {
      alive = false;
    };
  }, [injected, wanted]);
  return injected === undefined ? loaded : injected;
}
