import type { ScenePlan } from '@/features/plan/scenePlan.store';
import { threeToWorld, toThreePoint } from '@/features/viewer3d/worldCoordinates';
import {
  AmbientLight,
  Box3,
  OrthographicCamera,
  Plane,
  Scene,
  Vector3,
  WebGLRenderer,
  type Object3D,
} from 'three';

export function planBoundsOf(box: Box3): ScenePlan['bounds'] {
  const a = toThreePoint(box.min.toArray());
  const b = toThreePoint(box.max.toArray());
  if (!a || !b || box.isEmpty()) throw new Error('빈 Scene 범위');
  const min = threeToWorld(a);
  const max = threeToWorld(b);
  if (max[0] <= min[0] || min[1] <= max[1]) throw new Error('잘못된 Scene 범위');
  return { minX: min[0], maxX: max[0], minY: max[1], maxY: min[1] };
}

/** 원본 재질과 뷰어 문맥을 건드리지 않는 별도 문맥에서 한 장을 만든다. */
export function captureScenePlan(root: Object3D): ScenePlan {
  root.updateWorldMatrix(true, true);
  const box = new Box3().setFromObject(root);
  const bounds = planBoundsOf(box);
  const width = box.max.x - box.min.x;
  const depth = box.max.z - box.min.z;
  const renderer = new WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  try {
    renderer.setSize(
      Math.max(1, Math.round((768 * width) / Math.max(width, depth))),
      Math.max(1, Math.round((768 * depth) / Math.max(width, depth))),
      false,
    );
    renderer.clippingPlanes = [new Plane(new Vector3(0, -1, 0), 1.6)];
    const scene = new Scene();
    const copy = root.clone(true);
    copy.visible = true;
    scene.add(copy, new AmbientLight(0xffffff, 2));
    const camera = new OrthographicCamera(
      -width / 2,
      width / 2,
      depth / 2,
      -depth / 2,
      0.01,
      Math.max(100, box.max.y - box.min.y + 20),
    );
    camera.position.set(
      (box.min.x + box.max.x) / 2,
      Math.max(box.max.y, 1.6) + 5,
      (box.min.z + box.max.z) / 2,
    );
    camera.up.set(0, 0, -1);
    camera.lookAt(camera.position.x, 0, camera.position.z);
    renderer.render(scene, camera);
    return { imageUrl: renderer.domElement.toDataURL('image/png'), bounds };
  } finally {
    renderer.dispose();
    renderer.forceContextLoss();
  }
}

const cache = new Map<string, ScenePlan | null>();
export function cachedScenePlan(
  version: string,
  root: Object3D,
  capture = captureScenePlan,
): ScenePlan | null {
  if (cache.has(version)) return cache.get(version) ?? null;
  try {
    const plan = capture(root);
    cache.set(version, plan);
    return plan;
  } catch {
    console.warn('Scene 평면도를 만들지 못했습니다.');
    cache.set(version, null);
    return null;
  }
}
