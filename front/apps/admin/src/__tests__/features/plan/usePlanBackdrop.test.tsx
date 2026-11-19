import { ROOM_BOUNDS } from '@/features/plan/plan';
import { useScenePlanStore } from '@/features/plan/scenePlan.store';
import { usePlanBackdrop } from '@/features/plan/usePlanBackdrop';
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

afterEach(() => useScenePlanStore.getState().setPlan(null));
describe('Scene 평면도 바탕', () => {
  it('Scene 이 없거나 실패하면 기본 모눈을 쓴다', () => {
    const { result } = renderHook(() => usePlanBackdrop());
    expect(result.current).toEqual({ bounds: ROOM_BOUNDS, backdrop: undefined });
  });
  it('이미지와 World 범위를 함께 연결한다', () => {
    const { result } = renderHook(() => usePlanBackdrop());
    const plan = {
      imageUrl: 'data:image/png;base64,AA',
      bounds: { minX: -1, minY: -2, maxX: 3, maxY: 4 },
    };
    act(() => useScenePlanStore.getState().setPlan(plan));
    expect(result.current.bounds).toEqual(plan.bounds);
    expect(result.current.backdrop?.imageUrl).toBe(plan.imageUrl);
    act(() => useScenePlanStore.getState().setPlan(null));
    expect(result.current).toEqual({ bounds: ROOM_BOUNDS, backdrop: undefined });
  });
});
