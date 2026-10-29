import type { ViewerEditBox } from '@/features/viewer3d';
import { writeFieldValue } from '@/features/zones/zoneEditor';
import { describe, expect, it } from 'vitest';

const BOX: ViewerEditBox = { min: [1, 2, 0], max: [3, 5, 2] };

describe('writeFieldValue', () => {
  it('시작 X·Y 는 크기를 두고 옮기고, 폭·깊이는 시작 쪽을 고정한다', () => {
    expect(writeFieldValue(BOX, 'originX', 4)).toEqual({ min: [4, 2, 0], max: [6, 5, 2] });
    expect(writeFieldValue(BOX, 'originY', 0)).toEqual({ min: [1, 0, 0], max: [3, 3, 2] });
    expect(writeFieldValue(BOX, 'width', 5)).toEqual({ min: [1, 2, 0], max: [6, 5, 2] });
    expect(writeFieldValue(BOX, 'depth', 1)).toEqual({ min: [1, 2, 0], max: [3, 3, 2] });
  });

  it('하단·상단은 반대쪽을 고정하고, 크기가 0 이하이거나 하단이 상단 이상·바닥 아래면 고치지 않는다', () => {
    expect(writeFieldValue(BOX, 'zMin', 1)).toEqual({ min: [1, 2, 1], max: [3, 5, 2] });
    expect(writeFieldValue(BOX, 'zMax', 3)).toEqual({ min: [1, 2, 0], max: [3, 5, 3] });
    expect(writeFieldValue(BOX, 'width', 0)).toBeNull();
    expect(writeFieldValue(BOX, 'depth', -1)).toBeNull();
    expect(writeFieldValue(BOX, 'zMin', 2)).toBeNull();
    expect(writeFieldValue(BOX, 'zMin', -0.1)).toBeNull();
    expect(writeFieldValue(BOX, 'zMax', 0)).toBeNull();
  });
});
