import { type MockAccount, MOCK_ACCOUNTS } from '@/mocks/accounts';
import { createFloorPlanMock } from '@/mocks/floorPlan';
import { createHistoryMock } from '@/mocks/history';
import { createRulesMock } from '@/mocks/rules';
import { sceneHandlers } from '@/mocks/scenes';
import { createSetupMock } from '@/mocks/setup';
import { createStaticObjectsMock } from '@/mocks/staticObjects';
import { createTbmMock } from '@/mocks/tbm';
import { http, HttpResponse } from 'msw/http';

function readCredentials(body: unknown) {
  if (typeof body !== 'object' || body === null) return undefined;
  if (!('username' in body) || !('password' in body)) return undefined;
  const { username, password } = body;
  if (typeof username !== 'string' || typeof password !== 'string') return undefined;
  return { username, password };
}

// 서버 갱신 쿠키(simai_refresh, 14일) 흉내. 브라우저의 msw 는 응답 쿠키를 저장했다가 credentials: 'include' 요청의
// cookies 로 넘긴다(새로고침해도 남는다). 값은 계정 id 다.
const REFRESH_COOKIE = 'simai_refresh';
const REFRESH_MAX_AGE_S = 14 * 24 * 60 * 60;
const refreshCookie = (value: string, maxAge: number) =>
  `${REFRESH_COOKIE}=${value}; Path=/api/auth; SameSite=Strict; Max-Age=${maxAge}`;

// 발급마다 다른 토큰을 준다. 갱신한 토큰이 이전과 같으면 401 뒤 다시 보내기를 하지 않는다(lib/http).
let issued = 0;
const signedIn = (account: MockAccount) =>
  HttpResponse.json(
    {
      accessToken: `mock-token.${account.id}.${++issued}`,
      user: { id: account.id, role: account.role },
    },
    { headers: { 'Set-Cookie': refreshCookie(account.id, REFRESH_MAX_AGE_S) } },
  );

// 작업면은 앱이 살아 있는 동안 상태를 둔다. 구성 묶음 mock(startBackend 의 createZonesMock)이 발행 때 지금 작업면을 싣도록 같은 인스턴스를 나눈다.
export const staticObjectsMock = createStaticObjectsMock();
export const tbmMock = createTbmMock({ deferDaysToHistory: true });
export const setupMock = createSetupMock();

// 워치 배정(-342). 개발 mock 은 권한을 보지 않고 메모리에만 둔다(서버는 잇기·풀기를 관리자만 받는다).
const watchBindings = new Map<string, { world_id: string; bound_at: number }>();

// 경로 앞의 * 는 VITE_API_BASE_URL 이 무엇이든 맞추려는 것이다.
export const handlers = [
  http.get('*/api/sites/current', () =>
    HttpResponse.json({
      site_id: 'classroom-1',
      name: '교실 테스트베드',
      world_origin: null,
      established_calibration_version: 'cal-8',
      created_at_ms: 1_793_690_000_000,
    }),
  ),
  http.get('*/api/watch-bindings', () =>
    HttpResponse.json(
      [...watchBindings].map(([device_id, b], i) => ({
        binding_id: i + 1,
        device_id,
        world_id: b.world_id,
        method: 'MANUAL',
        bound_at: b.bound_at,
        channel_state: 'LOST',
      })),
    ),
  ),
  http.put('*/api/watch-bindings/:deviceId', async ({ params, request }) => {
    const body: unknown = await request.json().catch(() => undefined);
    const world =
      typeof body === 'object' && body !== null ? Reflect.get(body, 'world_id') : undefined;
    if (typeof world !== 'string' || world.trim() === '') {
      return HttpResponse.json(
        { status: 400, code: 'INVALID_WORLD_ID', detail: 'World ID 가 비었습니다' },
        { status: 400 },
      );
    }
    const device = String(params.deviceId);
    for (const [d, b] of watchBindings) if (b.world_id === world) watchBindings.delete(d);
    watchBindings.set(device, { world_id: world, bound_at: Date.now() });
    return HttpResponse.json({
      binding_id: 1,
      device_id: device,
      world_id: world,
      method: 'MANUAL',
      bound_at: Date.now(),
      channel_state: 'LOST',
    });
  }),
  http.delete('*/api/watch-bindings/:deviceId', ({ params }) =>
    watchBindings.delete(String(params.deviceId))
      ? new HttpResponse(null, { status: 204 })
      : HttpResponse.json(
          { status: 404, code: 'BINDING_NOT_FOUND', detail: '이 워치의 배정이 없습니다' },
          { status: 404 },
        ),
  ),
  http.post('*/api/auth/login', async ({ request }) => {
    const credentials = readCredentials(await request.json().catch(() => undefined));
    const account =
      credentials &&
      MOCK_ACCOUNTS.find(
        (a) => a.username === credentials.username && a.password === credentials.password,
      );
    if (!account) {
      return HttpResponse.json(
        { message: '아이디 또는 비밀번호가 맞지 않습니다' },
        { status: 401 },
      );
    }
    return signedIn(account);
  }),
  http.post('*/api/auth/refresh', ({ cookies }) => {
    const account = MOCK_ACCOUNTS.find((a) => a.id === cookies[REFRESH_COOKIE]);
    if (!account) {
      return HttpResponse.json(
        { status: 401, detail: '다시 로그인하세요', code: 'INVALID_REFRESH_TOKEN' },
        { status: 401, headers: { 'Set-Cookie': refreshCookie('', 0) } },
      );
    }
    return signedIn(account);
  }),
  http.post(
    '*/api/auth/logout',
    () => new HttpResponse(null, { status: 204, headers: { 'Set-Cookie': refreshCookie('', 0) } }),
  ),
  // 구성 묶음 발행·ACK 는 startBackend 의 구역 mock 이 맡는다.
  ...staticObjectsMock.handlers,
  ...sceneHandlers,
  ...createRulesMock().handlers,
  ...createFloorPlanMock().handlers,
  ...tbmMock.handlers,
  ...setupMock.handlers,
  // 히스토리 작업 목록·구간 기록(그때그때 합성한다).
  ...createHistoryMock().handlers,
];
