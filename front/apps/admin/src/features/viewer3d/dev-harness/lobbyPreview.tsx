import { worldPoint, worldToThree } from '@/features/viewer3d/worldCoordinates';
import { useThree } from '@react-three/fiber';
import { useEffect, useState } from 'react';
import type { PerspectiveCamera } from 'three';

// 실측 로비 재구성 프리뷰(-59 개발 검수) 전용. 재구성 GLB 옆 메타(JSON)에 적힌 실제 카메라 자세로 Viewer 카메라를 옮겨
// 같은 시각의 원본 프레임과 나란히 비교한다. 자세는 MapAnything 추정(실측 보정 아님)이다. 개발 화면에서만 쓴다.

/** 로컬 재구성 자산 위치(저장소 밖, node_modules 캐시라 Git 에 들어가지 않는다). XBot 과 같은 방식이다. */
export const DEV_LOBBY_GLB_URL = '/node_modules/.cache/spatial-dev/lobby-preview.glb';
export const DEV_LOBBY_META_URL = '/node_modules/.cache/spatial-dev/lobby-preview.json';

export interface DevCameraPose {
  /** World 위치(m). */
  position: [number, number, number];
  /** 보는 방향 단위 벡터(World). */
  forward: [number, number, number];
  /** 세로 화각(도). */
  fovYDeg: number;
}

/** ?camPose=cam_A 처럼 고른 카메라. 없으면 null. */
export function readCamPose(search: string): string | null {
  const v = new URLSearchParams(search).get('camPose');
  return v && /^[A-Za-z0-9_-]{1,32}$/.test(v) ? v : null;
}

/** 메타 JSON 의 cameras[camId]. 없거나 형식이 틀리면 null. */
export function useDevCameraPose(camId: string | null): DevCameraPose | null {
  const [pose, setPose] = useState<DevCameraPose | null>(null);
  useEffect(() => {
    if (!camId) return;
    let alive = true;
    void fetch(DEV_LOBBY_META_URL)
      .then((r) => (r.ok ? r.json() : null))
      .then((meta: { cameras?: Record<string, DevCameraPose> } | null) => {
        const p = meta?.cameras?.[camId];
        if (alive && p && p.position.length === 3 && p.forward.length === 3) setPose(p);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [camId]);
  return camId ? pose : null;
}

interface OrbitLike {
  target: { set(x: number, y: number, z: number): void };
  update(): void;
}

/**
 * 비교 시점의 근거리 자르기(m). 카메라가 달린 벽·기둥을 다른 카메라가 재구성하면 자세·축척 추정 오차로 카메라 바로 앞에 놓여
 * 시야를 가린다. 원본 사진과 비교할 때만 이만큼 앞부터 그린다.
 */
const COMPARE_NEAR_M = 0.8;

/** Canvas 안에서 카메라를 그 자세로 옮긴다(회전 중심은 보는 방향 3 m 앞). 한 번만 적용하고 이후 조작은 자유다. */
export function DevCameraPoseApplier({ pose }: { pose: DevCameraPose | null }) {
  const get = useThree((s) => s.get);
  const ready = useThree((s) => s.controls !== null);
  useEffect(() => {
    const controls = get().controls as unknown as OrbitLike | null;
    if (!pose || !ready || !controls) return;
    const camera = get().camera as PerspectiveCamera;
    const [x, y, z] = pose.position;
    const [fx, fy, fz] = pose.forward;
    const p = worldToThree(worldPoint(x, y, z));
    const t = worldToThree(worldPoint(x + 3 * fx, y + 3 * fy, z + 3 * fz));
    camera.position.set(p[0], p[1], p[2]);
    camera.fov = pose.fovYDeg;
    camera.near = COMPARE_NEAR_M;
    camera.updateProjectionMatrix();
    controls.target.set(t[0], t[1], t[2]);
    controls.update();
  }, [pose, ready, get]);
  return null;
}
