import { http, HttpResponse } from 'msw/http';

// 서버 구역 API(S15P31A101-120) 대신 쓰는 mock. 3D 뷰어 데모와 같은 직사각형(DEMO) 형식이다.
// 이벤트 시나리오(realtimeScenario)의 z-04·z-02 와 맞춘다. 위치는 디자인 데모의 기본 구역을 따른다.
export const MOCK_ZONES_RESPONSE = {
  contract: 'DEMO_UNCONFIRMED',
  zones: [
    {
      zone_id: 'z-04',
      name: '개구부 위 작업 공간',
      aabb: { min_xyz: [5.8, 1.2, 0], max_xyz: [8.6, 3.8, 2.4] },
    },
    {
      zone_id: 'z-02',
      name: '단상 40cm',
      aabb: { min_xyz: [1, 6.2, 0], max_xyz: [4, 8.4, 0.4] },
    },
  ],
};

export const zonesHandlers = [
  http.get('*/api/zones', () => HttpResponse.json(MOCK_ZONES_RESPONSE)),
];
