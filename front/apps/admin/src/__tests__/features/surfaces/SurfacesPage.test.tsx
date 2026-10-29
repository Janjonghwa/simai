import { SpatialViewportHost, useSpatialViewport } from '@/features/layout/SpatialViewportHost';
import {
  geometryErrors,
  newSurfaceGeometry,
  readWorkSurfaces,
  sameGeometry,
  withField,
  type SurfaceField,
} from '@/features/surfaces/surfaces';
import { SurfacesPage } from '@/features/surfaces/SurfacesPage';
import type { SurfaceGeometry, ViewerWorkSurfaces } from '@/features/surfaces/types';
import type {
  Viewer3DHandle,
  Viewer3DProps,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d';
import { sceneHandlers } from '@/mocks/scenes';
import { createStaticObjectsMock, MOCK_STATIC_OBJECTS } from '@/mocks/staticObjects';
import { createZonesMock } from '@/mocks/zones';
import { useObjectEditStore } from '@/store/objectEdit.store';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore, type Role } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { useImperativeHandle, useLayoutEffect, useRef, type ReactNode } from 'react';
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from 'vitest';

const DESK = 'ws-0a0c0002';
const STAGE = 'ws-0a0c0001';

const auth = vi.hoisted(() => ({ token: 'mock-token.admin' }));
vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => auth.token }) };
});
vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: [], loaded: true, failed: false }),
}));

// ── 3D 뷰어 대역 ─────────────────────────────────────────────────────────────
// 넣은 작업면을 장면으로 들고, 작업면 편집 세션(-188)을 흉내 낸다: 시작·만들기는 미리보기를 알리고, 칸 고침은 형상을 바꿔
// 다시 알리고, 끝내기·취소는 결과를 알린다(새 작업면은 상자가 사라지며 선택이 null 이 된다). 원본 형상이 바뀌면 STALE_SOURCE.
interface FakeSession {
  id: string;
  origin: 'REGISTERED' | 'NEW';
  before: SurfaceGeometry;
  draft: SurfaceGeometry;
}
const fake = vi.hoisted(() => ({
  props: null as unknown,
  scene: new Map<string, unknown>(),
  session: null as unknown,
  dragging: false,
  select: vi.fn(),
  focus: vi.fn(),
  beginEdit: vi.fn(),
  beginObjectCreate: vi.fn(),
  update: vi.fn(),
  finish: vi.fn(),
  cancel: vi.fn(),
}));
const props = () => fake.props as Viewer3DProps | null;
const scene = () => fake.scene as Map<string, SurfaceGeometry>;
const session = () => fake.session as FakeSession | null;
const objectTarget = (id: string) => ({ type: 'object', id }) as const;
const provenance = {
  sourceId: 's-1',
  geometryContract: 'OPERATIONAL_GEOMETRY_UNCONFIRMED',
  surfacesReceivedAt: 1,
  geometryVersion: null,
  sceneVersion: null,
  configVersion: null,
};

function emitPreview() {
  const s = session()!;
  props()?.onObjectEditPreview?.({
    objectKind: 'WORK_SURFACE',
    target: objectTarget(s.id),
    mode: 'LIVE',
    origin: s.origin,
    before: s.before,
    draft: s.draft,
    provenance,
    saved: false,
  } as never);
}
const cancelled = (reason: string) => {
  const s = session()!;
  return {
    objectKind: 'WORK_SURFACE',
    status: 'CANCELLED',
    target: objectTarget(s.id),
    mode: 'LIVE',
    origin: s.origin,
    before: s.before,
    reason,
    provenance,
    saved: false,
  };
};
function endSession(result: object) {
  const s = session()!;
  fake.session = null;
  props()?.onObjectEditResult?.(result as never);
  if (s.origin === 'NEW') props()?.onSelectionChange?.(null);
}

const handle = {
  select: (target: ViewerTarget) => {
    fake.select(target);
    if (target.type === 'object' && !scene().has(target.id) && session()?.id !== target.id)
      return { ok: false, reason: 'NOT_FOUND' };
    if (session() && session()!.id !== target.id) endSession(cancelled('SELECTION_CHANGED'));
    return { ok: true };
  },
  focus: (target: ViewerTarget) => {
    fake.focus(target);
    return { ok: true };
  },
  clearSelection: () => {
    if (session()) endSession(cancelled('SELECTION_CHANGED'));
    return { ok: true };
  },
  setMode: () => ({ ok: true }),
  beginEdit: (target: ViewerTarget) => {
    fake.beginEdit(target);
    const geometry = scene().get(target.id);
    if (!geometry) {
      props()?.onObjectEditResult?.({
        status: 'REJECTED',
        target,
        reason: 'NOT_FOUND',
        detail: '',
        saved: false,
      } as never);
      return { ok: false, reason: 'NOT_FOUND' };
    }
    if (session()?.id === target.id) return { ok: true };
    if (session()) endSession(cancelled('REPLACED'));
    fake.session = { id: target.id, origin: 'REGISTERED', before: geometry, draft: geometry };
    props()?.onSelectionChange?.(target);
    emitPreview();
    return { ok: true };
  },
  beginObjectCreate: ({ id, geometry }: { id: string; geometry: SurfaceGeometry }) => {
    fake.beginObjectCreate(id, geometry);
    if (scene().has(id) || session()?.id === id) return { ok: false, reason: 'INVALID_TARGET' };
    if (session()) endSession(cancelled('REPLACED'));
    fake.session = { id, origin: 'NEW', before: geometry, draft: geometry };
    props()?.onSelectionChange?.(objectTarget(id));
    emitPreview();
    return { ok: true };
  },
  updateObjectEditField: ({ field, value }: { field: SurfaceField; value: number }) => {
    fake.update(field, value);
    const s = session();
    if (!s) return { ok: false, reason: 'NOT_EDITABLE' };
    if (fake.dragging) return { ok: false, reason: 'NOT_READY' };
    const next = withField(s.draft, field, value);
    if (geometryErrors(next)) return { ok: false, reason: 'INVALID_GEOMETRY' };
    s.draft = next;
    emitPreview();
    return { ok: true };
  },
  finishObjectEdit: () => {
    fake.finish();
    const s = session();
    if (!s || fake.dragging) return null;
    const result = {
      objectKind: 'WORK_SURFACE',
      status: 'FINISHED',
      target: objectTarget(s.id),
      mode: 'LIVE',
      origin: s.origin,
      before: s.before,
      after: s.draft,
      afterItem: {},
      changed: !sameGeometry(s.before, s.draft),
      provenance,
      saved: false,
    };
    endSession(result);
    return result;
  },
  cancelObjectEdit: () => {
    fake.cancel();
    if (!session()) return null;
    const result = cancelled('USER');
    endSession(result);
    return result;
  },
};

function FakeViewer({ ref, ...viewerProps }: Viewer3DProps) {
  useImperativeHandle(ref, () => handle as unknown as Viewer3DHandle);
  const reported = useRef(false);
  useLayoutEffect(() => {
    fake.props = viewerProps;
    if (reported.current) return;
    reported.current = true;
    viewerProps.onStatusChange?.({ phase: 'ready', mode: 'LIVE', error: null } as ViewerStatus);
  });
  // 2D 로 바꾸면 뷰어가 내려가며 열린 편집을 UNMOUNTED 로 끝낸다.
  useLayoutEffect(
    () => () => {
      if (session()) endSession(cancelled('UNMOUNTED'));
    },
    [],
  );
  return <div data-testid="viewer3d" />;
}
const loadFake = () => Promise.resolve({ default: FakeViewer });

/** Source 대역: 넣은 작업면을 장면으로 바꾼다. 편집 중 원본 형상이 바뀌면 STALE_SOURCE 로 끝낸다. */
function receiveSurfaces(result: { raw: ViewerWorkSurfaces }) {
  const next = new Map<string, SurfaceGeometry>(
    result.raw.work_surfaces.map((w) => [
      w.surface_id,
      { centerXy: w.center_xy, sizeXy: w.size_xy, yawRad: w.yaw_rad, topHeightM: w.top_height_m },
    ]),
  );
  fake.scene = next;
  const s = session();
  if (s?.origin !== 'REGISTERED') return;
  const now = next.get(s.id);
  if (!now || !sameGeometry(now, s.before))
    endSession({
      status: 'REJECTED',
      target: objectTarget(s.id),
      reason: 'STALE_SOURCE',
      detail: '',
      saved: false,
    });
}

// ── 화면 ───────────────────────────────────────────────────────────────────
function KeyProbe() {
  return <span data-testid="key">{useSpatialViewport()?.selectedKey ?? ''}</span>;
}
const host = (withPage = true) => (
  <SpatialViewportHost loadViewer={loadFake}>
    <KeyProbe />
    {withPage && <SurfacesPage />}
  </SpatialViewportHost>
);

function signInAs(role: Role) {
  auth.token = `mock-token.${role.toLowerCase()}`;
  useSessionStore.setState({
    session: { accessToken: auth.token, user: { id: role.toLowerCase(), role } },
  });
}

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

let pushWorkSurfaces: Mock;
beforeEach(() => {
  signInAs('ADMIN');
  server.resetHandlers(
    ...sceneHandlers,
    ...createStaticObjectsMock().handlers,
    ...createZonesMock({ ackDelayMs: 200 }).handlers,
  );
  pushWorkSurfaces = vi.fn(receiveSurfaces);
  useRealtimeStore.setState(
    {
      ...useRealtimeStore.getInitialState(),
      source: { id: 's-1', pushZones: vi.fn(), pushWorkSurfaces } as never,
    },
    true,
  );
  useObjectEditStore.getState().clear();
  fake.props = null;
  fake.scene = new Map();
  fake.session = null;
  fake.dragging = false;
  for (const spy of [
    fake.select,
    fake.focus,
    fake.beginEdit,
    fake.beginObjectCreate,
    fake.update,
    fake.finish,
    fake.cancel,
  ])
    spy.mockReset();
});
afterEach(cleanup);

async function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(host(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  await screen.findByTestId('viewer3d');
  await screen.findByRole('button', { name: new RegExp(DESK) });
  await waitFor(() => expect(scene().size).toBe(2));
  return view;
}

const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement;
const row = (id: string) => screen.getByRole('button', { name: new RegExp(id) });
const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement;
const typeInto = (label: string, value: string) => {
  fireEvent.change(field(label), { target: { value } });
  fireEvent.blur(field(label));
};
/** 작업면 요청 본문을 남긴다(답은 원래 mock 이 한다). */
function recordWrites() {
  const puts: unknown[] = [];
  const posts: unknown[] = [];
  server.use(
    http.put('*/api/static-objects/:objectId', async ({ request }) => {
      puts.push(await request.clone().json());
      return undefined;
    }),
    http.post('*/api/static-objects', async ({ request }) => {
      posts.push(await request.clone().json());
      return undefined;
    }),
  );
  return { puts, posts };
}
/** 다른 관리자가 책상을 새 버전으로 저장한다(이름·상판 높이만 바꿀 수 있다). */
const otherAdminSaves = (id: string, { name = '책상', top = 0.74 } = {}) =>
  fetch(`http://api.test/api/static-objects/${id}`, {
    method: 'PUT',
    headers: { Authorization: 'Bearer mock-token.admin', 'Content-Type': 'application/json' },
    body: JSON.stringify({
      kind: 'WORK_SURFACE',
      name,
      obb: { center: [5, 4.6, top / 2], size: [1.2, 0.6, top], yaw_rad: -0.2618 },
      top_height_m: top,
      top_height_sigma_m: null,
    }),
  });
const otherAdminRenames = (id: string, name: string) => otherAdminSaves(id, { name });

describe('작업면 등록 화면', () => {
  it('목록을 보이고 작업면을 3D 에 넣으며, 행을 누르면 고르고 초점을 맞춰 값을 보인다', async () => {
    await renderPage();
    expect([...scene().keys()].sort()).toEqual([STAGE, DESK]);
    expect(within(row(DESK)).getByText('상판 0.74m')).toBeTruthy();
    // 읽는 이름은 이름·배지 사이를 끊는다.
    expect(screen.getByRole('button', { name: `${DESK} · 책상, 상판 0.74m` })).toBeTruthy();

    act(() => row(DESK).click());
    expect(row(DESK).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByTestId('key').textContent).toBe(`object:${DESK}`);
    expect(fake.focus).toHaveBeenCalledWith(objectTarget(DESK));
    expect(field('폭 (m)').value).toBe('1.20');
    expect(field('회전 (°)').value).toBe('-15.0');
    expect(button('저장').disabled).toBe(true);
  });

  it('반장은 값만 보고 고치지 못한다', async () => {
    signInAs('FOREMAN');
    await renderPage();
    act(() => row(STAGE).click());
    expect(screen.getByLabelText('작업면 값')).toBeTruthy();
    expect(screen.queryByLabelText('폭 (m)')).toBeNull();
    for (const name of ['새 작업면', '3D 로 고치기', '저장', '현장에 반영'])
      expect(screen.queryByRole('button', { name })).toBeNull();
  });

  it('3D 로 고치고 칸을 바꾸면 3D 에 넣고(회전은 rad), 저장하면 편집을 끝낸 뒤 새 버전을 보낸다', async () => {
    const writes = recordWrites();
    await renderPage();
    act(() => row(DESK).click());
    act(() => button('3D 로 고치기').click());
    expect(fake.beginEdit).toHaveBeenCalledWith(objectTarget(DESK));
    expect(button('3D 편집 끝내기')).toBeTruthy();

    act(() => typeInto('회전 (°)', '30'));
    expect(fake.update).toHaveBeenLastCalledWith('yaw', expect.closeTo(Math.PI / 6, 6));
    expect(field('회전 (°)').value).toBe('30.0');
    expect(within(row(DESK)).getByText('고치는 중')).toBeTruthy();

    act(() => button('저장').click());
    expect(fake.finish).toHaveBeenCalled();
    await screen.findByText('저장했습니다 · v2');
    expect(writes.puts).toHaveLength(1);
    const body = writes.puts[0] as {
      name: string | null;
      obb: { center: number[]; size: number[]; yaw_rad: number };
      top_height_m: number;
    };
    expect(body.name).toBe('책상');
    expect(body.obb.yaw_rad).toBeCloseTo(Math.PI / 6);
    expect(body.obb.size[2]).toBeCloseTo(0.74);
    expect(body.obb.center[2]).toBeCloseTo(0.37);
    expect(body.top_height_m).toBeCloseTo(0.74);
    await waitFor(() => expect(within(row(DESK)).queryByText('고치는 중')).toBeNull());
    expect(useObjectEditStore.getState().kept).toEqual({});
  });

  it('3D 편집 없이 칸을 고쳐도 초안으로 남고, 저장하면 그 값을 보낸다', async () => {
    const writes = recordWrites();
    await renderPage();
    act(() => row(DESK).click());
    act(() => typeInto('폭 (m)', '1.5'));
    expect(fake.update).not.toHaveBeenCalled();
    expect(field('폭 (m)').value).toBe('1.50');
    expect(within(row(DESK)).getByText('고치는 중')).toBeTruthy();

    act(() => button('저장').click());
    await screen.findByText('저장했습니다 · v2');
    expect(fake.finish).not.toHaveBeenCalled();
    expect((writes.puts[0] as { obb: { size: number[] } }).obb.size[0]).toBe(1.5);
  });

  it('남긴 초안을 3D 로 다시 열면 바뀐 칸을 다시 넣고, Esc 로 그 편집을 버려도 초안은 남는다', async () => {
    await renderPage();
    act(() => row(DESK).click());
    act(() => typeInto('폭 (m)', '1.5'));
    act(() => button('3D 로 고치기').click());
    expect(fake.update).toHaveBeenCalledWith('width', 1.5);
    expect(field('폭 (m)').value).toBe('1.50');

    // 캔버스 Esc(USER).
    act(() => void handle.cancelObjectEdit());
    expect(field('폭 (m)').value).toBe('1.50');
    expect(within(row(DESK)).getByText('고치는 중')).toBeTruthy();
  });

  it('되돌리기는 열린 편집을 취소하고 초안을 버린다', async () => {
    await renderPage();
    act(() => row(DESK).click());
    act(() => typeInto('폭 (m)', '1.5'));
    act(() => button('3D 로 고치기').click());
    act(() => button('되돌리기').click());
    expect(fake.cancel).toHaveBeenCalled();
    expect(field('폭 (m)').value).toBe('1.20');
    expect(within(row(DESK)).queryByText('고치는 중')).toBeNull();
    expect(button('저장').disabled).toBe(true);
  });

  it('그 사이 다른 관리자가 저장했으면 보내지 않고 알린 뒤, 다시 저장하면 보낸다', async () => {
    const writes = recordWrites();
    await renderPage();
    act(() => row(DESK).click());
    act(() => typeInto('폭 (m)', '1.5'));
    await otherAdminRenames(DESK, '다른 사람');
    const sentBefore = writes.puts.length;

    act(() => button('저장').click());
    await screen.findByText(/그 사이 다른 버전이 저장되어/);
    expect(writes.puts.length).toBe(sentBefore);
    expect(field('폭 (m)').value).toBe('1.50');

    act(() => button('저장').click());
    await screen.findByText('저장했습니다 · v3');
    expect(writes.puts.length).toBe(sentBefore + 1);
  });

  it('다른 작업면을 저장하며 목록이 새로 와도, 고치기 시작한 버전으로 확인해 남의 저장을 덮지 않는다', async () => {
    const writes = recordWrites();
    await renderPage();
    act(() => row(DESK).click());
    act(() => typeInto('폭 (m)', '1.5'));
    await otherAdminRenames(DESK, '다른 사람');
    // 단상 저장의 저장 전 확인이 목록을 새로 받아 책상도 v2 가 된다.
    act(() => row(STAGE).click());
    fireEvent.change(field('이름'), { target: { value: '큰 단상' } });
    act(() => button('저장').click());
    await screen.findByText('저장했습니다 · v2');
    const sentBefore = writes.puts.length;

    act(() => row(DESK).click());
    expect(within(row(DESK)).getByText(/다른 사람/)).toBeTruthy();
    act(() => button('저장').click());
    await screen.findByText(/그 사이 다른 버전이 저장되어/);
    expect(writes.puts.length).toBe(sentBefore);
  });

  it('그 사이 다른 관리자가 형상을 바꿨으면, 다시 저장할 때 내가 고친 칸만 그 버전에 얹어 보낸다', async () => {
    const writes = recordWrites();
    await renderPage();
    act(() => row(DESK).click());
    act(() => typeInto('폭 (m)', '1.5'));
    await otherAdminSaves(DESK, { top: 0.9 });

    act(() => button('저장').click());
    await screen.findByText(/그 사이 다른 버전이 저장되어/);
    expect(field('폭 (m)').value).toBe('1.50');
    expect(field('상판 높이 (m)').value).toBe('0.90');

    const sentBefore = writes.puts.length;
    act(() => button('저장').click());
    await screen.findByText('저장했습니다 · v3');
    const body = writes.puts[sentBefore] as { obb: { size: number[] }; top_height_m: number };
    expect(body.obb.size[0]).toBe(1.5);
    expect(body.top_height_m).toBeCloseTo(0.9);
  });

  it('이름만 바꿔도 저장하고, 공백만 넣으면 이름 없음으로 보낸다', async () => {
    const writes = recordWrites();
    await renderPage();
    act(() => row(STAGE).click());
    expect(button('저장').disabled).toBe(true);
    fireEvent.change(field('이름'), { target: { value: '   ' } });
    expect(button('저장').disabled).toBe(false);

    act(() => button('저장').click());
    await screen.findByText('저장했습니다 · v2');
    expect((writes.puts[0] as { name: unknown }).name).toBeNull();
  });

  it('새 작업면은 3D 에 상자를 띄우고, 저장하면 편집을 끝낸 뒤 만들어 새 작업면을 고른다', async () => {
    const writes = recordWrites();
    await renderPage();
    act(() => button('새 작업면').click());
    act(() => button('새 작업면').click());
    expect(fake.beginObjectCreate).toHaveBeenCalledTimes(1);
    expect(fake.beginObjectCreate).toHaveBeenCalledWith(
      'new-surface',
      newSurfaceGeometry(readWorkSurfaces(MOCK_STATIC_OBJECTS)),
    );
    expect(screen.getByRole('button', { name: /^새 작업면, 저장 전$/ })).toBeTruthy();
    fireEvent.change(field('이름'), { target: { value: '교탁' } });

    act(() => button('저장').click());
    expect(fake.finish).toHaveBeenCalled();
    const notice = await screen.findByText(/^만들었습니다 · ws-/);
    const id = notice.textContent!.replace('만들었습니다 · ', '');
    expect((writes.posts[0] as { name: string }).name).toBe('교탁');
    await waitFor(() => expect(fake.focus).toHaveBeenCalledWith(objectTarget(id)));
    expect(screen.getByTestId('key').textContent).toBe(`object:${id}`);
    expect(screen.queryByRole('button', { name: /^새 작업면, 저장 전$/ })).toBeNull();
    expect(row(id).getAttribute('aria-pressed')).toBe('true');
  });

  it('새 작업면을 만들지 못하면 알리고 초안을 남겨 3D 로 다시 열 수 있다', async () => {
    await renderPage();
    server.use(
      http.post('*/api/static-objects', () =>
        HttpResponse.json(
          { status: 400, code: 'INVALID_STATIC_OBJECT', detail: 'x' },
          { status: 400 },
        ),
      ),
    );
    act(() => button('새 작업면').click());
    act(() => typeInto('상판 높이 (m)', '0.9'));
    act(() => button('저장').click());
    await screen.findByText('작업면 값이 올바르지 않아 저장하지 못했습니다.');
    expect(screen.getByRole('button', { name: /^새 작업면, 저장 전$/ })).toBeTruthy();
    expect(field('상판 높이 (m)').value).toBe('0.90');

    act(() => button('3D 로 고치기').click());
    expect(fake.beginObjectCreate).toHaveBeenLastCalledWith(
      'new-surface',
      expect.objectContaining({ topHeightM: 0.9 }),
    );
  });

  it('2D 에서도 새 작업면 칸을 보이고(3D 로 고치기는 끔) 만들 수 있다', async () => {
    const writes = recordWrites();
    await renderPage();
    act(() => row(DESK).click());
    // 3D 가 멈추면 2D 로 바뀐다(손으로 2D 로 바꾸는 버튼은 없다).
    act(() =>
      (fake.props as Viewer3DProps | null)?.onStatusChange?.({
        phase: 'error',
        mode: 'LIVE',
        error: { code: 'RENDER_FAILED', message: 'x' },
      } as ViewerStatus),
    );
    act(() => button('새 작업면').click());
    expect(fake.beginObjectCreate).not.toHaveBeenCalled();
    expect(button('3D 로 고치기').disabled).toBe(true);
    expect(screen.getByTestId('key').textContent).toBe('');

    act(() => button('저장').click());
    await screen.findByText(/^만들었습니다 · ws-/);
    expect(writes.posts).toHaveLength(1);
  });

  it('저장하는 동안 버튼을 잠그고, 같은 틱에 두 번 눌러도 한 번만 보낸다', async () => {
    const writes = recordWrites();
    await renderPage();
    act(() => row(DESK).click());
    act(() => typeInto('폭 (m)', '1.5'));
    act(() => {
      button('저장').click();
      button('저장').click();
    });
    expect(button('저장 중…').disabled).toBe(true);
    expect(button('3D 로 고치기').disabled).toBe(true);
    expect(field('폭 (m)').disabled).toBe(true);
    await screen.findByText('저장했습니다 · v2');
    expect(writes.puts).toHaveLength(1);
  });

  it('반영할 수 없는 칸 값은 칸을 떠날 때 알리고 초안에 넣지 않으며, Escape 는 입력을 거둔다', async () => {
    await renderPage();
    act(() => row(DESK).click());
    act(() => typeInto('폭 (m)', '0'));
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toBe('폭과 깊이는 0 보다 커야 합니다.');
    // 오류는 칸 이름에 섞지 않고 설명으로만 잇는다.
    expect(field('폭 (m)').getAttribute('aria-describedby')).toBe(alert.id);
    act(() => typeInto('깊이 (m)', 'abc'));
    expect(screen.getByText('숫자를 입력하세요.')).toBeTruthy();
    expect(useObjectEditStore.getState().kept).toEqual({});

    fireEvent.keyDown(field('폭 (m)'), { key: 'Escape' });
    expect(field('폭 (m)').value).toBe('1.20');
  });

  it('3D 가 시작을 거부하면 이유를 알린다', async () => {
    await renderPage();
    act(() => row(DESK).click());
    scene().delete(DESK);
    act(() => button('3D 로 고치기').click());
    expect(
      screen.getByText('3D 장면에 아직 이 작업면이 없습니다. 잠시 뒤 다시 시도하세요.'),
    ).toBeTruthy();
  });

  it('편집 중 원본이 바뀌어 3D 편집이 멈추면 알리고, 고친 값은 남긴다', async () => {
    await renderPage();
    act(() => row(DESK).click());
    act(() => button('3D 로 고치기').click());
    act(() => typeInto('폭 (m)', '1.5'));
    const moved = MOCK_STATIC_OBJECTS.map((o) => ({
      surface_id: o.object_id,
      top_height_m: o.top_height_m,
      center_xy: [o.obb.center[0] + 1, o.obb.center[1]] as [number, number],
      size_xy: [o.obb.size[0], o.obb.size[1]] as [number, number],
      yaw_rad: o.obb.yaw_rad,
    }));
    act(() => receiveSurfaces({ raw: { work_surfaces: moved } }));
    expect(
      screen.getByText('작업면 목록이 바뀌어 3D 편집을 멈췄습니다. 고친 값은 남겨 두었습니다.'),
    ).toBeTruthy();
    expect(field('폭 (m)').value).toBe('1.50');
    expect(button('3D 로 고치기')).toBeTruthy();

    // 다른 일을 하면(되돌리기) 지난 경고는 지운다.
    act(() => button('되돌리기').click());
    expect(
      screen.queryByText('작업면 목록이 바뀌어 3D 편집을 멈췄습니다. 고친 값은 남겨 두었습니다.'),
    ).toBeNull();
  });

  it('저장한 작업면은 현장 미반영으로 세고, 현장에 반영하면 보내는 중이 된다', async () => {
    await renderPage();
    act(() => row(DESK).click());
    act(() => typeInto('폭 (m)', '1.5'));
    act(() => button('저장').click());
    await screen.findByText('저장했습니다 · v2');
    await screen.findByText('현장 미반영 1건');
    expect(within(row(DESK)).getByText('발행 전')).toBeTruthy();

    act(() => button('현장에 반영').click());
    await screen.findByText('현장에 보내는 중');
  });

  it('화면을 떠나면 편집을 취소하고 초안을 버리며, 3D 에서 작업면을 지우고 고른 작업면을 놓는다', async () => {
    const view = await renderPage();
    act(() => row(DESK).click());
    act(() => button('3D 로 고치기').click());
    act(() => typeInto('폭 (m)', '1.5'));

    view.rerender(host(false));
    expect(fake.cancel).toHaveBeenCalled();
    expect(useObjectEditStore.getState()).toMatchObject({ preview: null, kept: {}, ended: null });
    expect(pushWorkSurfaces).toHaveBeenLastCalledWith({ ok: true, raw: { work_surfaces: [] } });
    expect(screen.getByTestId('key').textContent).toBe('');
  });
});
