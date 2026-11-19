import { cachedScenePlan, planBoundsOf } from '@/features/viewer3d/scene/scenePlan';
import { Box3, BoxGeometry, Mesh, MeshBasicMaterial, Vector3 } from 'three';
import { describe, expect, it, vi } from 'vitest';

describe('Scene 평면도', () => {
  it('Three 범위를 북쪽 위 World 범위로 변환한다', () => {
    expect(planBoundsOf(new Box3(new Vector3(-2, 0, -8), new Vector3(4, 3, 5)))).toEqual({
      minX: -2,
      maxX: 4,
      minY: -5,
      maxY: 8,
    });
  });
  it('버전마다 한 번 찍고 원본 재질을 바꾸지 않는다', () => {
    const material = new MeshBasicMaterial();
    const root = new Mesh(new BoxGeometry(), material);
    const plan = {
      imageUrl: 'data:image/png;base64,AA',
      bounds: { minX: 0, minY: 0, maxX: 1, maxY: 1 },
    };
    const capture = vi.fn(() => plan);
    expect(cachedScenePlan('test-cache-version', root, capture)).toEqual(plan);
    expect(cachedScenePlan('test-cache-version', root, capture)).toEqual(plan);
    expect(capture).toHaveBeenCalledTimes(1);
    expect(root.material).toBe(material);
    expect(material.clippingPlanes).toBeNull();
  });
  it('실패하면 빈 결과를 유지한다', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(
      cachedScenePlan('test-failure-version', new Mesh(), () => {
        throw new Error();
      }),
    ).toBeNull();
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });
});
