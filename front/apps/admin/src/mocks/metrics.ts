import { readMockUser } from '@/mocks/mockUser';
import { http, HttpResponse } from 'msw/http';

// 서버 시스템 지표 API(S15P31A101-70) 대신 쓰는 mock. 형식은 features/system/types.ts 의 가정을 따른다.
// 값은 명세 6.6 목표(현장 p95 900ms, 관제 화면 1.2초, 반장 PWA 2.5초) 안쪽에서 조금씩 바뀐다.

export const metricsHandlers = [
  http.get('*/api/metrics', ({ request }) => {
    const user = readMockUser(request.headers.get('Authorization'));
    if (!user) {
      return HttpResponse.json(
        { status: 401, detail: '로그인이 필요합니다', code: 'UNAUTHORIZED' },
        { status: 401 },
      );
    }
    if (user.role !== 'ADMIN') {
      return HttpResponse.json(
        { status: 403, detail: '권한이 없습니다', code: 'FORBIDDEN' },
        { status: 403 },
      );
    }
    const wave = Math.floor(Date.now() / 1000) % 5;
    return HttpResponse.json({
      contract: 'DEMO_UNCONFIRMED',
      as_of: Date.now(),
      stages: {
        capture_to_site_alert_ms: { p50: 340 + wave * 5, p95: 520 + wave * 10 },
        capture_to_remote_delivery_ms: {
          viewer: { p50: 620 + wave * 10, p95: 980 + wave * 20 },
          pwa: { p50: 1400 + wave * 20, p95: 2100 + wave * 40 },
        },
      },
    });
  }),
];
