import { requestZones } from '@/features/zones/api';
import { readZones } from '@/features/zones/zones';
import { createHttp } from '@/lib/http';
import { MOCK_ZONES_RESPONSE, zonesHandlers } from '@/mocks/zones';
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

const box = (zone_id: string, min: number[], max: number[]) => ({
  zone_id,
  name: zone_id,
  aabb: { min_xyz: min, max_xyz: max },
});

describe('readZones', () => {
  it('직사각형 구역을 World 좌표 그대로 읽는다', () => {
    expect(
      readZones({ contract: 'DEMO_UNCONFIRMED', zones: [box('z-1', [1, 2, 0], [3, 4, 2])] }),
    ).toEqual([{ zoneId: 'z-1', name: 'z-1', min: [1, 2, 0], max: [3, 4, 2] }]);
  });

  it('형식이 틀렸거나 범위가 뒤집혔거나 0 인 구역, 같은 ID 가 둘 이상인 구역은 뺀다', () => {
    const zones = readZones({
      contract: 'DEMO_UNCONFIRMED',
      zones: [
        box('ok', [0, 0, 0], [1, 1, 1]),
        box('flat', [0, 0, 0], [1, 1, 0]),
        box('flip', [2, 0, 0], [1, 1, 1]),
        { zone_id: 'no-box', name: 'x' },
        box('dup', [0, 0, 0], [1, 1, 1]),
        box('dup', [2, 2, 0], [3, 3, 1]),
      ],
    });
    expect(zones.map((z) => z.zoneId)).toEqual(['ok']);
  });

  it('형식이 틀린 항목도 ID 를 세고(3D 뷰어와 같은 규칙), ID 는 자르지 않는다', () => {
    const zones = readZones({
      contract: 'DEMO_UNCONFIRMED',
      zones: [
        box('z', [0, 0, 0], [1, 1, 1]),
        { zone_id: 'z', name: 5 },
        box('a ', [0, 0, 0], [1, 1, 1]),
        box('a', [2, 2, 0], [3, 3, 1]),
        box('  ', [0, 0, 0], [1, 1, 1]),
      ],
    });
    expect(zones.map((z) => z.zoneId)).toEqual(['a ', 'a']);
  });
});

const BASE = 'http://api.test';
const server = setupServer(...zonesHandlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

describe('requestZones', () => {
  it('mock 구역(이벤트 시나리오의 z-04·z-02)을 받아 검사한다', async () => {
    const response = await requestZones(createHttp({ baseUrl: BASE }));
    expect(response).toEqual(MOCK_ZONES_RESPONSE);
    expect(readZones(response).map((z) => z.zoneId)).toEqual(['z-04', 'z-02']);
  });
});
