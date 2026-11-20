import { useZoneHoverStore } from '@/store/zoneHover.store';
import { afterEach, describe, expect, it } from 'vitest';

afterEach(() => {
  useZoneHoverStore.setState({ zoneId: null });
});

describe('zoneHover.store', () => {
  it('초기값은 null 이고 setZoneId 로 호버 구역 ID 를 바꾸고 비운다', () => {
    expect(useZoneHoverStore.getState().zoneId).toBeNull();

    useZoneHoverStore.getState().setZoneId('z-04');
    expect(useZoneHoverStore.getState().zoneId).toBe('z-04');

    useZoneHoverStore.getState().setZoneId(null);
    expect(useZoneHoverStore.getState().zoneId).toBeNull();
  });
});
