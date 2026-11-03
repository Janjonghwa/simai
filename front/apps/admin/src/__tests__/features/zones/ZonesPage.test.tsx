import type { ViewerEditBox } from '@/features/viewer3d';
import type { ZoneCommitRequest } from '@/features/zones/types';
import { ARM_MS } from '@/features/zones/useArmedDelete';
import { NEW_KEY } from '@/features/zones/zoneEditModel';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { createStaticObjectsMock } from '@/mocks/staticObjects';
import { createZonesMock, type ZonesMock } from '@/mocks/zones';
import { useBoxEditStore } from '@/store/boxEdit.store';
import { useSessionStore, type Role } from '@/store/session.store';
import { HISTORY_LIMIT, useZoneEditStore } from '@/store/zoneEdit.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return {
    api: createHttp({
      baseUrl: 'http://api.test',
      getToken: () => 'mock-token.admin',
    }),
  };
});

// 3D 뷰어 호스트 대신 선택만 들고 있는 작은 대역을 쓴다. 구역 목록은 실제 useZones 가 받는다.
const viewport = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  return {
    selected: null as string | null,
    listeners,
    emit: () => listeners.forEach((fn) => fn()),
  };
});
vi.mock('@/features/layout/SpatialViewportHost', async () => {
  const { useSyncExternalStore } = await import('react');
  const { useZones } = await vi.importActual<typeof import('@/features/zones/useZones')>(
    '@/features/zones/useZones',
  );
  return {
    useSpatialViewport: () => {
      const selected = useSyncExternalStore(
        (fn) => {
          viewport.listeners.add(fn);
          return () => viewport.listeners.delete(fn);
        },
        () => viewport.selected,
      );
      const { zones, raw, failed } = useZones();
      return {
        zones,
        zonesRaw: raw,
        zonesFailed: failed,
        selectedZoneId: selected,
        view: '2d',
        viewerReady: false,
        selectTarget: (target: { id: string }) => {
          viewport.selected = target.id;
          viewport.emit();
        },
        clearSelection: () => {
          viewport.selected = null;
          viewport.emit();
        },
      };
    },
  };
});

const server = setupServer();
const log: string[] = [];
/** 서버가 받은 POST /api/zones/commit 본문(받은 순서). */
const commits: ZoneCommitRequest[] = [];
let zonesMock: ZonesMock;

/** commit 본문을 기록하고 다음 처리기에 넘긴다(응답은 mock 이나 그 앞에 둔 처리기가 만든다). */
const recordCommit = async (request: Request) => {
  commits.push((await request.clone().json()) as ZoneCommitRequest);
};

beforeAll(() => {
  server.listen({ onUnhandledFrame: 'error' });
  server.events.on('request:start', ({ request }) => {
    const path = new URL(request.url).pathname;
    if (request.method !== 'GET') log.push(`${request.method} ${path}`);
    else if (path === '/api/zones') log.push('GET /api/zones');
  });
});
afterAll(() => server.close());

function signInAs(role: Role) {
  useSessionStore.setState({
    session: { accessToken: 'mock-token.admin', user: { id: 'admin', role } },
  });
}

beforeEach(() => {
  const staticObjects = createStaticObjectsMock();
  zonesMock = createZonesMock({ staticObjects: staticObjects.store });
  server.resetHandlers(
    http.post('*/api/zones/commit', ({ request }) => recordCommit(request).then(() => undefined)),
    ...zonesMock.handlers,
    ...staticObjects.handlers,
  );
  log.splice(0, log.length);
  commits.splice(0, commits.length);
  viewport.selected = null;
  viewport.listeners.clear();
  useZoneEditStore.getState().clear();
  useBoxEditStore.getState().clear();
  signInAs('ADMIN');
});
afterEach(() => {
  cleanup();
  useSessionStore.setState({ session: undefined });
});

async function renderPage({ surfaces = true }: { surfaces?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <ZonesPage />
    </QueryClientProvider>,
  );
  await screen.findByRole('option', { name: /z-04/ });
  // 작업면 목록과 구성 묶음 목록이 도착해야 이은 작업면·발행 전이 맞다.
  if (surfaces) await waitFor(() => expect(client.getQueryData(['static-objects'])).toBeDefined());
  await waitFor(() => expect(client.getQueryData(['config-bundles'])).toBeDefined());
  return view;
}

const picker = () => screen.getByRole('combobox', { name: '구역 선택' }) as HTMLSelectElement;
const optionTexts = () =>
  within(picker())
    .getAllByRole('option')
    .map((o) => o.textContent);
const pick = (value: string) => act(() => void fireEvent.change(picker(), { target: { value } }));
const button = (name: string | RegExp) => screen.getByRole('button', { name });
const writes = () => log.filter((entry) => !entry.startsWith('GET'));
const COMMIT = 'POST /api/zones/commit';
const AUTH = { Authorization: 'Bearer mock-token.admin' };
type Json = Record<string, unknown>;
/** 서버(mock)가 지금 가진 목록을 직접 받는다. */
const fetchList = async (path: string) =>
  (await (await fetch(`http://api.test${path}`, { headers: AUTH })).json()) as Json[];
const sendJson = (method: string, path: string, body: unknown) =>
  fetch(`http://api.test${path}`, {
    method,
    headers: { ...AUTH, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
/** 받은 작업면 항목을 요청 본문(StaticObjectBody)으로. */
const surfaceBodyOf = (item: Json) => ({
  kind: item.kind,
  name: item.name,
  obb: item.obb,
  top_height_m: item.top_height_m,
  top_height_sigma_m: null,
});
const deskOf = async () =>
  (await fetchList('/api/static-objects')).find((o) => o.object_id === 'ws-0a0c0002')!;
/** 다음 /commit 요청을 기록하고 이 상태·오류 이름으로 거절한다. */
const failCommit = (status: number, code: string) =>
  server.use(
    http.post('*/api/zones/commit', async ({ request }) => {
      await recordCommit(request);
      return HttpResponse.json({ status, detail: 'x', code }, { status });
    }),
  );
const surfaceDeleteButton = () =>
  within(screen.getByRole('region', { name: '고소 작업면' })).getByRole('button', { name: '삭제' });
const SHAPE_LOCKED_TEXT = '직사각형이 아니거나 높이가 없는 구역은 형상을 고칠 수 없습니다.';
const nameInput = () => screen.getByRole('textbox', { name: '이름' }) as HTMLInputElement;
const severitySelect = () => screen.getByRole('combobox', { name: '심각도' }) as HTMLSelectElement;
/** Ctrl+Z. 돌려주는 값은 브라우저 기본 동작을 막지 않았는지(막았으면 false). */
const pressUndo = (target: Window | Element = window, init: KeyboardEventInit = {}) =>
  fireEvent.keyDown(target, { code: 'KeyZ', key: 'z', ctrlKey: true, ...init });
const rename = (name: string) => fireEvent.change(nameInput(), { target: { value: name } });
const dragTo = (box: ViewerEditBox) => act(() => useBoxEditStore.getState().onChange(box));
const releasePointer = () => act(() => void window.dispatchEvent(new Event('pointerup')));
/** 3D 위에서 포인터를 누른다(끌기의 시작). 3D 가 아닌 body 에서 올려 캡처 처리기가 받는지도 본다. */
const pressPointer = () =>
  act(() => void document.body.dispatchEvent(new Event('pointerdown', { bubbles: true })));
const Z02_BOX: ViewerEditBox = { min: [1, 6.2, 0], max: [4, 8.4, 0.4] };

describe('구역 편집 화면', () => {
  it('드롭다운은 「새 구역 추가」가 먼저이고 기존 구역은 `ID · 이름`이다. 고르는 칸은 이름과 심각도뿐이다', async () => {
    await renderPage();
    expect(optionTexts()).toEqual(['새 구역 추가', 'z-04 · 개구부 구역', 'z-02 · 단상 구역']);
    expect(picker().value).toBe('__new__');
    expect(screen.getByRole('textbox', { name: '이름' })).toBeTruthy();
    const severity = screen.getByRole('combobox', { name: '심각도' });
    expect(
      within(severity)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['위험', '경고', '주의', '안내']);
    // 구역 종류·판정 유형은 화면에 없다.
    expect(screen.queryByLabelText('판정 유형')).toBeNull();
    expect(screen.queryByLabelText('유형')).toBeNull();
    expect(button('구역 추가')).toBeTruthy();
    expect(button('+ 고소 작업면 추가')).toBeTruthy();
  });

  it('새 구역은 OPENING·VOLUME_3D 로 만들고 저장과 발행을 /commit 한 번(CREATE, publish)으로 보낸다', async () => {
    await renderPage();
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
      target: { value: '새로 만든 구역' },
    });
    fireEvent.change(screen.getByRole('combobox', { name: '심각도' }), {
      target: { value: 'CAUTION' },
    });
    act(() => button('구역 추가').click());
    await waitFor(() => expect(picker().value).toBe('z-00000001'));
    expect(writes()).toEqual([COMMIT]);
    expect(commits).toHaveLength(1);
    expect(commits[0]).toMatchObject({
      zone: {
        op: 'CREATE',
        body: {
          name: '새로 만든 구역',
          zone_kind: 'OPENING',
          eval_type: 'VOLUME_3D',
          severity: 'CAUTION',
          z_min_m: 0,
          z_max_m: 2,
        },
      },
      publish: true,
    });
    // 새 구역에는 ID 도 작업면도 보내지 않는다.
    expect(commits[0]!.zone).not.toHaveProperty('zone_id');
    expect(commits[0]).not.toHaveProperty('surface');
    expect(await fetchList('/api/zones')).toContainEqual(
      expect.objectContaining({ zone_id: 'z-00000001', name: '새로 만든 구역' }),
    );
    // 만든 구역은 바로 고를 수 있고, 새 구역 초안은 비었으며, 발행 전이 아니다(응답의 묶음이 목록에 들어갔다).
    expect(optionTexts()).toContain('z-00000001 · 새로 만든 구역');
    expect(useZoneEditStore.getState().drafts[NEW_KEY]).toBeUndefined();
    expect(screen.queryByRole('button', { name: '다시 보내기' })).toBeNull();
  });

  it('새 구역에 작업면을 더하면 구역 CREATE + 작업면 CREATE 를 한 요청으로 보내고, 만든 작업면은 서버 연결로 보인다', async () => {
    await renderPage();
    act(() => button('+ 고소 작업면 추가').click());
    expect(screen.getByLabelText('높이(m)')).toHaveProperty('value', '0.40');
    act(() => button('구역 추가').click());
    await waitFor(() => expect(picker().value).toBe('z-00000001'));
    expect(writes()).toEqual([COMMIT]);
    expect(commits).toHaveLength(1);
    expect(commits[0]).toMatchObject({
      zone: { op: 'CREATE' },
      surface: { op: 'CREATE', body: { kind: 'WORK_SURFACE', top_height_m: 0.4 } },
      publish: true,
    });
    expect(commits[0]!.surface).not.toHaveProperty('object_id');
    // 서버가 새 작업면을 새 구역에 이었다.
    expect(await fetchList('/api/static-objects')).toContainEqual(
      expect.objectContaining({ kind: 'WORK_SURFACE', zone_id: 'z-00000001', object_version: 1 }),
    );
    // 초안은 모두 지웠고, 작업면 카드는 서버가 이은 작업면으로 그려진다.
    expect(useZoneEditStore.getState().drafts).toEqual({});
    await waitFor(() => expect(screen.getByLabelText('높이(m)')).toHaveProperty('value', '0.40'));
    expect(optionTexts()).toContain('z-00000001 · 새 구역');
  });

  it('이은 작업면(서버 zone_id)이 카드에 보이고, 3D 손잡이는 구역·작업면을 전환해 고친다', async () => {
    await renderPage();
    pick('z-02');
    const height = await screen.findByLabelText('높이(m)');
    expect(height).toHaveProperty('value', '0.40');
    // 구역 상자를 고치는 손잡이
    expect(useBoxEditStore.getState().boxEdit).toMatchObject({
      id: 'z-02',
      kind: 'zone',
      box: { min: [1, 6.2, 0], max: [4, 8.4, 0.4] },
    });
    expect(button('구역').getAttribute('aria-pressed')).toBe('true');
    act(() => button('작업면').click());
    expect(button('작업면').getAttribute('aria-pressed')).toBe('true');
    // 작업면 상자(바닥 ~ 상판)로 바뀌고 아래쪽 화살표는 쓰지 않는 종류다.
    expect(useBoxEditStore.getState().boxEdit).toMatchObject({
      id: 'surface:z-02',
      kind: 'surface',
      box: { min: [1, 6.2, 0], max: [4, 8.4, 0.4] },
    });
    act(() => button('구역').click());
    expect(useBoxEditStore.getState().boxEdit?.kind).toBe('zone');
  });

  it('작업면이 없는 구역은 추가 버튼만 보이고, 추가하면 구역 상자 가운데에 놓고 작업면 손잡이로 넘긴다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    expect(screen.queryByLabelText('높이(m)')).toBeNull();
    act(() => button('+ 고소 작업면 추가').click());
    expect(screen.getByLabelText('높이(m)')).toHaveProperty('value', '0.40');
    expect(useBoxEditStore.getState().boxEdit).toMatchObject({
      id: 'surface:z-04',
      kind: 'surface',
      box: { min: [6.7, 2, 0], max: [7.7, 3, 0.4] },
    });
    expect(optionTexts()).toContain('z-04 · 개구부 구역 · 저장 전');
  });

  it('구역과 작업면은 서버가 저장한 zone_id 로만 이어진다: 위치가 구역 안이어도 zone_id 가 없으면 잇지 않는다', async () => {
    // 책상(ws-0a0c0002, zone_id 없음)을 z-04 바닥(X 5.8~8.6, Y 1.2~3.8) 안으로 옮긴다. PUT 은 zone_id 를 빼면 연결을 그대로 둔다.
    const desk = await deskOf();
    await sendJson('PUT', '/api/static-objects/ws-0a0c0002', {
      ...surfaceBodyOf(desk),
      obb: { center: [7, 2.5, 0.37], size: [1.2, 0.6, 0.74], yaw_rad: 0 },
    });
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    expect(screen.queryByLabelText('높이(m)')).toBeNull();
  });

  it('zone_id 가 있는 작업면은 중심이 구역 밖이어도 그 구역의 작업면 카드로 보이고 경고하지 않는다', async () => {
    // /commit 으로 책상(중심 (5, 4.6)은 z-04 밖)을 z-04 에 잇는다.
    const desk = await deskOf();
    const linked = await sendJson('POST', '/api/zones/commit', {
      zone: { op: 'KEEP', zone_id: 'z-04' },
      surface: { op: 'UPDATE', object_id: 'ws-0a0c0002', body: surfaceBodyOf(desk) },
      publish: false,
    });
    expect(linked.status).toBe(200);
    commits.splice(0, commits.length);
    await renderPage();
    pick('z-04');
    expect(await screen.findByLabelText('높이(m)')).toHaveProperty('value', '0.74');
    expect(screen.queryByText('작업면 중심이 구역 안에 있어야 합니다.')).toBeNull();
    // 이은 작업면이 있어 추가 버튼은 없다.
    expect(screen.queryByRole('button', { name: '+ 고소 작업면 추가' })).toBeNull();
  });

  it('고친 구역은 `저장 전`이고, 3D 에서 구역을 옮기면 이은 작업면도 같이 옮겨 저장을 막지 않는다', async () => {
    await renderPage();
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    expect(optionTexts()).toContain('z-02 · 단상 구역');
    // 구역을 +1m(X) 옮긴다(손잡이를 끌어 놓은 결과를 뷰어가 올린 것처럼).
    act(() => useBoxEditStore.getState().onChange({ min: [2, 6.2, 0], max: [5, 8.4, 0.4] }));
    expect(optionTexts()).toContain('z-02 · 단상 구역 · 저장 전');
    expect(screen.queryByText('작업면 중심이 구역 안에 있어야 합니다.')).toBeNull();
    expect((button('저장') as HTMLButtonElement).disabled).toBe(false);
    // 작업면 쪽 상자는 같이 옮겨졌다.
    act(() => button('작업면').click());
    expect(useBoxEditStore.getState().boxEdit?.box).toEqual({
      min: [2, 6.2, 0],
      max: [5, 8.4, 0.4],
    });
  });

  it('구역을 옮겨 저장하면 같이 옮겨진 작업면도 한 요청(구역 UPDATE + 작업면 UPDATE)으로 보낸다', async () => {
    await renderPage();
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    act(() => useBoxEditStore.getState().onChange({ min: [2, 6.2, 0], max: [5, 8.4, 0.4] }));
    act(() => button('저장').click());
    await waitFor(() => expect(optionTexts()).toContain('z-02 · 단상 구역'));
    expect(writes()).toEqual([COMMIT]);
    expect(commits).toHaveLength(1);
    expect(commits[0]).toMatchObject({
      zone: { op: 'UPDATE', zone_id: 'z-02' },
      surface: {
        op: 'UPDATE',
        object_id: 'ws-0a0c0001',
        body: { obb: { center: [3.5, 7.3, expect.closeTo(0.2, 5)] } },
      },
      publish: true,
    });
    // 구역·작업면 모두 새 버전이 되고, 작업면은 여전히 z-02 에 이어져 있다.
    expect(await fetchList('/api/zones')).toContainEqual(
      expect.objectContaining({ zone_id: 'z-02', zone_version: 2 }),
    );
    expect(await fetchList('/api/static-objects')).toContainEqual(
      expect.objectContaining({ object_id: 'ws-0a0c0001', object_version: 2, zone_id: 'z-02' }),
    );
    expect(screen.getByLabelText('높이(m)')).toHaveProperty('value', '0.40');
  });

  it('작업면을 고치면서 구역을 줄여 작업면 중심이 밖이 되면 알리고 저장을 막는다', async () => {
    await renderPage();
    pick('z-02');
    const height = await screen.findByLabelText('높이(m)');
    // 폭을 1 m 로 줄이면 작업면 중심(X 2.5)이 구역(X 1~2) 밖에 남는다. 작업면을 고치지 않았으면 막지 않는다.
    act(() => useBoxEditStore.getState().onChange({ min: [1, 6.2, 0], max: [2, 8.4, 0.4] }));
    expect(screen.queryByText('작업면 중심이 구역 안에 있어야 합니다.')).toBeNull();
    // 작업면을 고치면(새 버전을 보낼 때) 중심이 구역 안에 있어야 한다.
    fireEvent.change(height, { target: { value: '0.5' } });
    fireEvent.keyDown(height, { key: 'Enter' });
    expect(screen.getByText('작업면 중심이 구역 안에 있어야 합니다.')).toBeTruthy();
    expect((button('저장') as HTMLButtonElement).disabled).toBe(true);
    expect(writes()).toEqual([]);
  });

  it('구역만 고치면 작업면 중심이 구역 밖이 되어도 저장을 막지 않고 구역만 보낸다(이은 연결은 서버가 정한다)', async () => {
    await renderPage();
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    // 폭을 1 m 로 줄이면 작업면 중심(X 2.5)이 구역(X 1~2) 밖에 남지만, 작업면은 고치지 않았다.
    act(() => useBoxEditStore.getState().onChange({ min: [1, 6.2, 0], max: [2, 8.4, 0.4] }));
    expect(screen.queryByText('작업면 중심이 구역 안에 있어야 합니다.')).toBeNull();
    expect((button('저장') as HTMLButtonElement).disabled).toBe(false);
    act(() => button('저장').click());
    await waitFor(() => expect(optionTexts()).toContain('z-02 · 단상 구역'));
    expect(writes()).toEqual([COMMIT]);
    expect(commits[0]).toMatchObject({
      zone: { op: 'UPDATE', zone_id: 'z-02' },
      publish: true,
    });
    expect(commits[0]).not.toHaveProperty('surface');
    // 작업면은 새 버전 없이 그대로 z-02 에 이어져 있다.
    expect(await fetchList('/api/static-objects')).toContainEqual(
      expect.objectContaining({ object_id: 'ws-0a0c0001', object_version: 1, zone_id: 'z-02' }),
    );
    expect(screen.getByLabelText('높이(m)')).toHaveProperty('value', '0.40');
  });

  it('되돌리기는 이름·모양·작업면의 저장 전 고침을 모두 버린다', async () => {
    await renderPage();
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
      target: { value: '다른 이름' },
    });
    act(() => useBoxEditStore.getState().onChange({ min: [2, 6.2, 0], max: [5, 8.4, 0.4] }));
    expect((button('되돌리기') as HTMLButtonElement).disabled).toBe(false);
    act(() => button('되돌리기').click());
    expect(screen.getByRole('textbox', { name: '이름' })).toHaveProperty('value', '단상 구역');
    expect(optionTexts()).toContain('z-02 · 단상 구역');
    expect(useBoxEditStore.getState().boxEdit?.box.min).toEqual([1, 6.2, 0]);
  });

  it('작업면 목록을 받기 전에는 저장할 수 없다(이은 작업면을 모른 채 보내지 않는다)', async () => {
    let open = () => {};
    const gate = new Promise<void>((resolve) => {
      open = resolve;
    });
    server.use(
      http.get('*/api/static-objects', async () => {
        await gate;
        return undefined;
      }),
    );
    await renderPage({ surfaces: false });
    pick('z-04');
    await screen.findByRole('textbox', { name: '이름' });
    rename('개구부 B');
    expect((button('저장') as HTMLButtonElement).disabled).toBe(true);
    open();
    await waitFor(() => expect((button('저장') as HTMLButtonElement).disabled).toBe(false));
  });

  it('구역 이름만 고쳐 저장하면 저장 전 확인(GET) 뒤 /commit 한 번(구역 UPDATE, 작업면 없음, publish)을 보내고 초안을 지운다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    rename('개구부 B');
    expect(optionTexts()).toContain('z-04 · 개구부 구역 · 저장 전');
    log.splice(0, log.length);
    act(() => button('저장').click());
    // 저장 전 · 발행 전 표시가 모두 사라진다.
    await waitFor(() => expect(optionTexts()).toContain('z-04 · 개구부 B'));
    expect(log).toEqual(['GET /api/zones', COMMIT]);
    expect(commits).toHaveLength(1);
    expect(commits[0]).toMatchObject({
      zone: { op: 'UPDATE', zone_id: 'z-04', body: { name: '개구부 B' } },
      publish: true,
    });
    expect(commits[0]).not.toHaveProperty('surface');
    expect(useZoneEditStore.getState().drafts['z-04']).toBeUndefined();
    expect(screen.queryByRole('button', { name: '다시 보내기' })).toBeNull();
  });

  it('구역·작업면을 함께 고쳐 저장하면 구역 UPDATE + 작업면 UPDATE 한 요청이고, 저장하면 저장 전·발행 전이 사라진다', async () => {
    await renderPage();
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
      target: { value: '단상 B' },
    });
    const height = screen.getByLabelText('높이(m)');
    fireEvent.change(height, { target: { value: '0.6' } });
    fireEvent.keyDown(height, { key: 'Enter' });
    log.splice(0, log.length);
    act(() => button('저장').click());
    await waitFor(() => expect(optionTexts()).toContain('z-02 · 단상 B'));
    // 저장 전 확인(구역·작업면 GET) 뒤 요청 하나다.
    expect(writes()).toEqual([COMMIT]);
    expect(log.filter((entry) => entry === 'GET /api/zones')).toHaveLength(1);
    expect(commits).toHaveLength(1);
    expect(commits[0]).toMatchObject({
      zone: { op: 'UPDATE', zone_id: 'z-02', body: { name: '단상 B' } },
      surface: { op: 'UPDATE', object_id: 'ws-0a0c0001', body: { top_height_m: 0.6 } },
      publish: true,
    });
    expect(useZoneEditStore.getState().drafts['z-02']).toBeUndefined();
    expect(await fetchList('/api/static-objects')).toContainEqual(
      expect.objectContaining({
        object_id: 'ws-0a0c0001',
        object_version: 2,
        top_height_m: expect.closeTo(0.6, 5),
      }),
    );
    expect(Number((screen.getByLabelText('높이(m)') as HTMLInputElement).value)).toBeCloseTo(0.6);
    expect(screen.queryByRole('button', { name: '다시 보내기' })).toBeNull();
  });

  it('작업면만 고치면 구역은 KEEP 이라 새 버전을 만들지 않는다', async () => {
    await renderPage();
    pick('z-02');
    const height = await screen.findByLabelText('높이(m)');
    fireEvent.change(height, { target: { value: '0.5' } });
    fireEvent.keyDown(height, { key: 'Enter' });
    act(() => button('저장').click());
    await waitFor(() => expect(optionTexts()).toContain('z-02 · 단상 구역'));
    expect(writes()).toEqual([COMMIT]);
    expect(commits).toHaveLength(1);
    expect(commits[0]).toMatchObject({
      zone: { op: 'KEEP', zone_id: 'z-02' },
      surface: { op: 'UPDATE', object_id: 'ws-0a0c0001', body: { top_height_m: 0.5 } },
      publish: true,
    });
    expect(commits[0]!.zone).not.toHaveProperty('body');
    expect(await fetchList('/api/zones')).toContainEqual(
      expect.objectContaining({ zone_id: 'z-02', zone_version: 1 }),
    );
    expect(await fetchList('/api/static-objects')).toContainEqual(
      expect.objectContaining({ object_id: 'ws-0a0c0001', object_version: 2, top_height_m: 0.5 }),
    );
    expect(Number((screen.getByLabelText('높이(m)') as HTMLInputElement).value)).toBe(0.5);
  });

  it('작업면이 없는 구역에 작업면을 더해 저장하면 구역 KEEP + 작업면 CREATE 한 요청이고 서버 연결로 이어진다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    act(() => button('+ 고소 작업면 추가').click());
    act(() => button('저장').click());
    await waitFor(() => expect(optionTexts()).toContain('z-04 · 개구부 구역'));
    expect(writes()).toEqual([COMMIT]);
    expect(commits).toHaveLength(1);
    expect(commits[0]).toMatchObject({
      zone: { op: 'KEEP', zone_id: 'z-04' },
      surface: { op: 'CREATE', body: { kind: 'WORK_SURFACE', top_height_m: 0.4 } },
      publish: true,
    });
    expect(await fetchList('/api/static-objects')).toContainEqual(
      expect.objectContaining({ kind: 'WORK_SURFACE', zone_id: 'z-04', object_version: 1 }),
    );
    expect(await fetchList('/api/zones')).toContainEqual(
      expect.objectContaining({ zone_id: 'z-04', zone_version: 1 }),
    );
    // 초안은 지워졌고 작업면 손잡이 모드도 풀렸으며, 카드는 서버가 이은 작업면으로 남는다.
    expect(useZoneEditStore.getState().drafts['z-04']).toBeUndefined();
    await waitFor(() => expect(useBoxEditStore.getState().boxEdit?.kind).toBe('zone'));
    expect(screen.getByLabelText('높이(m)')).toHaveProperty('value', '0.40');
  });

  it('이은 작업면을 지우고 저장하면 구역 KEEP + 작업면 ARCHIVE 한 요청이다', async () => {
    await renderPage();
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    act(() => surfaceDeleteButton().click());
    expect(screen.queryByLabelText('높이(m)')).toBeNull();
    expect(optionTexts()).toContain('z-02 · 단상 구역 · 저장 전');
    act(() => button('저장').click());
    await waitFor(() => expect(optionTexts()).toContain('z-02 · 단상 구역'));
    expect(writes()).toEqual([COMMIT]);
    expect(commits).toEqual([
      {
        zone: { op: 'KEEP', zone_id: 'z-02' },
        surface: { op: 'ARCHIVE', object_id: 'ws-0a0c0001' },
        publish: true,
      },
    ]);
    // 보관한 작업면은 목록에서 빠지고 구역에는 다시 추가 버튼이 보인다.
    expect((await fetchList('/api/static-objects')).map((o) => o.object_id)).toEqual([
      'ws-0a0c0002',
    ]);
    expect(await screen.findByRole('button', { name: '+ 고소 작업면 추가' })).toBeTruthy();
    expect(useZoneEditStore.getState().drafts['z-02']).toBeUndefined();
  });

  it('작업면을 지우고 다시 추가해 저장하면 구역 KEEP + 작업면 REPLACE(옛 작업면 보관 뒤 새로 잇기) 한 요청이다', async () => {
    await renderPage();
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    act(() => surfaceDeleteButton().click());
    act(() => button('+ 고소 작업면 추가').click());
    expect(optionTexts()).toContain('z-02 · 단상 구역 · 저장 전');
    act(() => button('저장').click());
    await waitFor(() => expect(optionTexts()).toContain('z-02 · 단상 구역'));
    expect(writes()).toEqual([COMMIT]);
    expect(commits).toHaveLength(1);
    expect(commits[0]).toMatchObject({
      zone: { op: 'KEEP', zone_id: 'z-02' },
      surface: {
        op: 'REPLACE',
        object_id: 'ws-0a0c0001',
        body: { kind: 'WORK_SURFACE', obb: { size: [1, 1, 0.4] } },
      },
      publish: true,
    });
    // 옛 작업면은 보관되고, 새 작업면(v1)이 z-02 에 이어진다. 구역에는 지금 작업면이 하나다.
    const surfaces = await fetchList('/api/static-objects');
    expect(surfaces.map((o) => o.object_id)).not.toContain('ws-0a0c0001');
    const ofZone = surfaces.filter((o) => o.zone_id === 'z-02');
    expect(ofZone).toHaveLength(1);
    expect(ofZone[0]).toMatchObject({ object_version: 1, obb: { size: [1, 1, 0.4] } });
    expect(useZoneEditStore.getState().drafts['z-02']).toBeUndefined();
    expect(screen.getByLabelText('높이(m)')).toHaveProperty('value', '0.40');
  });

  it('다른 관리자가 먼저 저장해 /commit 이 409(CONCURRENT_EDIT)면 최신 구역을 다시 받고, 초안은 남기고, 묶음 없이 다시 보내지 않는다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    failCommit(409, 'CONCURRENT_EDIT');
    rename('내 이름');
    log.splice(0, log.length);
    act(() => button('저장').click());
    expect(
      await screen.findByText(
        '그 사이 다른 버전이 저장되어 최신 구역을 다시 받았습니다. 확인한 뒤 다시 저장하세요.',
      ),
    ).toBeTruthy();
    // 저장 전 확인(GET) → /commit(409) → 항목을 다시 받는다(GET). 묶음 오류가 아니라 publish:false 로 다시 보내지 않는다.
    await waitFor(() =>
      expect(log.filter((e) => e === 'GET /api/zones').length).toBeGreaterThanOrEqual(2),
    );
    expect(writes()).toEqual([COMMIT]);
    expect(commits).toHaveLength(1);
    expect(commits[0]).toMatchObject({ zone: { op: 'UPDATE', zone_id: 'z-04' }, publish: true });
    expect(log).not.toContain('POST /api/config-bundles');
    expect(optionTexts()).toContain('z-04 · 개구부 구역 · 저장 전');
    expect(screen.getByRole('textbox', { name: '이름' })).toHaveProperty('value', '내 이름');
    expect(useZoneEditStore.getState().drafts['z-04']).toBeDefined();
  });

  it('고치는 사이 다른 관리자가 새 버전을 저장했으면 /commit 을 보내지 않고 최신 구역을 보이며, 초안은 남긴다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    rename('내 이름');
    // 다른 관리자가 z-04 의 새 버전(v2)을 저장한다.
    const z04 = (await fetchList('/api/zones')).find((z) => z.zone_id === 'z-04')!;
    await sendJson('PUT', '/api/zones/z-04', {
      name: '다른 사람',
      zone_kind: z04.zone_kind,
      eval_type: z04.eval_type,
      polygon_xy: z04.polygon_xy,
      z_min_m: z04.z_min_m,
      z_max_m: z04.z_max_m,
      severity: z04.severity,
      has_guardrail: z04.has_guardrail,
      requirements: z04.requirements,
    });
    log.splice(0, log.length);
    act(() => button('저장').click());
    expect(
      await screen.findByText(
        '그 사이 다른 버전이 저장되어 최신 구역을 다시 받았습니다. 확인한 뒤 다시 저장하세요.',
      ),
    ).toBeTruthy();
    // 지금 버전을 확인하는 GET 만 보내고 /commit 은 보내지 않았다. 확인에서 받은 최신 구역이 화면에 들어갔다.
    expect(log).toEqual(['GET /api/zones']);
    expect(commits).toEqual([]);
    await waitFor(() => expect(optionTexts()).toContain('z-04 · 다른 사람 · 저장 전'));
    expect(nameInput().value).toBe('내 이름');
  });

  it('묶음을 못 만들면(409) 같은 본문을 publish:false 로 한 번 더 보내 저장만 하고, 한 줄·다시 보내기를 띄운다. 발행 전은 묶음 items 로 센다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    zonesMock.failNextPublish('CALIBRATION_MISSING');
    rename('개구부 B');
    act(() => button('저장').click());
    const alert = await screen.findByText('저장했지만 현장 판정으로 보내지 못했습니다.');
    expect(alert).toBeTruthy();
    expect(screen.getByText('보정 문서가 없어 현장에 반영할 수 없습니다.')).toBeTruthy();
    // publish:true 가 409 로 되돌려져, 같은 본문을 묶음 없이(publish:false) 다시 보냈다.
    expect(writes()).toEqual([COMMIT, COMMIT]);
    expect(commits.map((c) => c.publish)).toEqual([true, false]);
    expect(commits[1]).toEqual({ ...commits[0]!, publish: false });
    // 저장은 됐으니 초안은 지워졌고, 새 버전(v2)은 마지막 묶음(cfg-12)의 items 에 없어 발행 전이다.
    expect(useZoneEditStore.getState().drafts['z-04']).toBeUndefined();
    await waitFor(() => expect(optionTexts()).toContain('z-04 · 개구부 B · 발행 전'));
    expect(await fetchList('/api/zones')).toContainEqual(
      expect.objectContaining({ zone_id: 'z-04', zone_version: 2 }),
    );
    // 다시 보내기는 구성 묶음 발행 하나다(구역·작업면은 다시 보내지 않는다).
    log.splice(0, log.length);
    act(() => button('다시 보내기').click());
    await waitFor(() => expect(writes()).toEqual(['POST /api/config-bundles']));
    // 보내는 동안에는 띠만 숨고, 성공하면 알림도 사라진다.
    await waitFor(() =>
      expect(screen.queryByText('보정 문서가 없어 현장에 반영할 수 없습니다.')).toBeNull(),
    );
    expect(screen.queryByText('저장했지만 현장 판정으로 보내지 못했습니다.')).toBeNull();
    await waitFor(() => expect(optionTexts()).toContain('z-04 · 개구부 B'));
    expect(commits).toHaveLength(2);
  });

  it.each([
    ['CURRENT_CALIBRATION_MISSING', '유효한 보정 문서가 없어 현장에 반영할 수 없습니다.'],
    ['RULES_MISSING', '유효한 규칙 버전이 없어 현장에 반영할 수 없습니다.'],
    ['PUBLIC_URL_NOT_SET', '서버 공개 주소가 설정되지 않아 현장에 반영할 수 없습니다.'],
  ] as const)(
    '묶음을 못 만드는 이유가 %s 여도 저장은 되고, 이유를 알리고 다시 보내기를 띄운다',
    async (code, reason) => {
      await renderPage();
      pick('z-04');
      await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
      zonesMock.failNextPublish(code);
      rename('개구부 B');
      act(() => button('저장').click());
      expect(await screen.findByText('저장했지만 현장 판정으로 보내지 못했습니다.')).toBeTruthy();
      expect(screen.getByText(reason)).toBeTruthy();
      expect(commits.map((c) => c.publish)).toEqual([true, false]);
      await waitFor(() => expect(optionTexts()).toContain('z-04 · 개구부 B · 발행 전'));
      expect(button('다시 보내기')).toBeTruthy();
    },
  );

  it('묶음 오류가 아닌 서버 오류(SITE_NOT_SINGLE)는 다시 보내지 않고 저장 실패로 알리며, 아무것도 저장되지 않고 초안은 남는다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    zonesMock.failNextPublish('SITE_NOT_SINGLE');
    rename('개구부 B');
    act(() => button('저장').click());
    expect(await screen.findByText('현장이 하나로 정해지지 않아 저장할 수 없습니다.')).toBeTruthy();
    expect(commits.map((c) => c.publish)).toEqual([true]);
    expect(screen.queryByText('저장했지만 현장 판정으로 보내지 못했습니다.')).toBeNull();
    // 서버가 구역 저장까지 되돌렸다(v1 그대로). 초안은 그대로 남는다.
    expect(await fetchList('/api/zones')).toContainEqual(
      expect.objectContaining({ zone_id: 'z-04', zone_version: 1, name: '개구부 구역' }),
    );
    await waitFor(() => expect(optionTexts()).toContain('z-04 · 개구부 구역 · 저장 전'));
    expect(nameInput().value).toBe('개구부 B');
  });

  it(
    '삭제는 두 번 눌러야 한다: 첫 클릭은 「삭제 확인」(3초 뒤 되돌아간다), 두 번째가 /commit 한 번(구역 ARCHIVE, 이은 작업면은 서버가 함께 보관)',
    async () => {
      await renderPage();
      pick('z-02');
      await screen.findByLabelText('높이(m)');
      const deleteButton = () => screen.getAllByRole('button', { name: /^삭제( 확인)?$/ }).at(-1)!;
      act(() => deleteButton().click());
      expect(deleteButton().textContent).toBe('삭제 확인');
      expect(writes()).toEqual([]);
      await waitFor(() => expect(deleteButton().textContent).toBe('삭제'), {
        timeout: ARM_MS + 1500,
      });
      expect(writes()).toEqual([]);

      act(() => deleteButton().click());
      act(() => deleteButton().click());
      await waitFor(() => expect(optionTexts()).toEqual(['새 구역 추가', 'z-04 · 개구부 구역']));
      expect(writes()).toEqual([COMMIT]);
      // 작업면 op 는 붙이지 않는다.
      expect(commits).toEqual([{ zone: { op: 'ARCHIVE', zone_id: 'z-02' }, publish: true }]);
      // 서버가 이은 작업면도 보관했고(책상만 남는다), 묶음에서도 빠져 다시 보내기가 없다.
      expect((await fetchList('/api/static-objects')).map((o) => o.object_id)).toEqual([
        'ws-0a0c0002',
      ]);
      expect(screen.queryByRole('button', { name: '다시 보내기' })).toBeNull();
      // 삭제한 뒤에는 새 구역 양식으로 돌아간다.
      expect(picker().value).toBe('__new__');
      expect(useZoneEditStore.getState().drafts['z-02']).toBeUndefined();
    },
    ARM_MS + 8000,
  );

  it('작업면이 없는 구역을 삭제해도 구역 ARCHIVE 한 요청(publish)만 보낸다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    const deleteButton = () => button(/^삭제( 확인)?$/);
    act(() => deleteButton().click());
    act(() => deleteButton().click());
    await waitFor(() => expect(optionTexts()).toEqual(['새 구역 추가', 'z-02 · 단상 구역']));
    expect(writes()).toEqual([COMMIT]);
    expect(commits).toEqual([{ zone: { op: 'ARCHIVE', zone_id: 'z-04' }, publish: true }]);
  });

  it('직사각형이 아닌 구역은 형상을 고칠 수 없다는 알림이 뜨고 3D 손잡이를 띄우지 않는다. 속성은 고칠 수 있다', async () => {
    await fetch('http://api.test/api/zones', {
      method: 'POST',
      headers: { Authorization: 'Bearer mock-token.admin', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: '삼각형 구역',
        zone_kind: 'OPENING',
        eval_type: 'VOLUME_3D',
        polygon_xy: [
          [1, 1],
          [3, 1],
          [2, 3],
        ],
        z_min_m: 0,
        z_max_m: 2,
        severity: 'HIGH',
        has_guardrail: null,
        requirements: null,
      }),
    });
    await renderPage();
    pick('z-00000001');
    expect(await screen.findByText(SHAPE_LOCKED_TEXT)).toBeTruthy();
    expect(useBoxEditStore.getState().boxEdit).toBeNull();
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
      target: { value: '삼각 B' },
    });
    expect((button('저장') as HTMLButtonElement).disabled).toBe(false);
    // 직사각형 구역에서는 보이지 않는다.
    pick('z-04');
    await waitFor(() => expect(screen.queryByText(SHAPE_LOCKED_TEXT)).toBeNull());
    expect(useBoxEditStore.getState().boxEdit?.kind).toBe('zone');
  });

  it('관리자가 아니면 값만 보이고 저장·삭제·추가 버튼과 3D 손잡이가 없다', async () => {
    signInAs('VIEWER');
    await renderPage();
    expect(screen.queryByRole('button', { name: '구역 추가' })).toBeNull();
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    expect(screen.queryByRole('button', { name: '저장' })).toBeNull();
    expect(screen.queryByRole('button', { name: '삭제' })).toBeNull();
    expect(screen.queryByRole('button', { name: '작업면' })).toBeNull();
    // 이름 칸은 칸 묶음(fieldset)이 잠근다.
    expect(screen.getByRole('textbox', { name: '이름' }).matches(':disabled')).toBe(true);
    expect(useBoxEditStore.getState().boxEdit).toBeNull();
  });

  it('구역을 바꿔도 고치던 초안은 남는다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
      target: { value: '고치는 중' },
    });
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    expect(optionTexts()).toContain('z-04 · 개구부 구역 · 저장 전');
    pick('z-04');
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: '이름' })).toHaveProperty('value', '고치는 중'),
    );
  });
  describe('Ctrl+Z 되돌리기', () => {
    it('이름·심각도를 고친 것을 한 단계씩 되돌린다', async () => {
      await renderPage();
      pick('z-04');
      await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
      const savedName = nameInput().value;
      const savedSeverity = severitySelect().value;
      rename('다른 이름');
      fireEvent.change(severitySelect(), {
        target: { value: savedSeverity === 'CAUTION' ? 'INFO' : 'CAUTION' },
      });

      expect(pressUndo()).toBe(false);
      expect(severitySelect().value).toBe(savedSeverity);
      expect(nameInput().value).toBe('다른 이름');
      expect(optionTexts()).toContain('z-04 · 개구부 구역 · 저장 전');

      pressUndo();
      expect(nameInput().value).toBe(savedName);
      expect(optionTexts()).toContain('z-04 · 개구부 구역');

      // 더 되돌릴 것이 없으면 브라우저 기본 동작을 막지 않는다.
      expect(pressUndo()).toBe(true);
      expect(nameInput().value).toBe(savedName);
    });

    it('이름을 연달아 입력한 것은 한 단계이고, 다른 고침이 끼면 새 단계다', async () => {
      await renderPage();
      pick('z-04');
      await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
      const savedName = nameInput().value;
      rename('개');
      rename('개구');
      rename('개구부 B');
      fireEvent.change(severitySelect(), { target: { value: 'INFO' } });
      rename('개구부 C');
      expect(useZoneEditStore.getState().history['z-04']).toHaveLength(3);

      pressUndo();
      expect(nameInput().value).toBe('개구부 B');
      pressUndo();
      pressUndo();
      expect(nameInput().value).toBe(savedName);
    });

    it('3D 손잡이로 끈 것은 포인터를 놓을 때까지 한 단계다', async () => {
      await renderPage();
      pick('z-02');
      await screen.findByLabelText('높이(m)');
      rename('다른 이름');
      dragTo({ min: [1.5, 6.2, 0], max: [4.5, 8.4, 0.4] });
      dragTo({ min: [2, 6.2, 0], max: [5, 8.4, 0.4] });
      dragTo({ min: [2.5, 6.2, 0], max: [5.5, 8.4, 0.4] });
      releasePointer();
      expect(useZoneEditStore.getState().history['z-02']).toHaveLength(2);

      pressUndo();
      expect(useBoxEditStore.getState().boxEdit?.box).toEqual(Z02_BOX);
      expect(nameInput().value).toBe('다른 이름');
      expect(optionTexts()).toContain('z-02 · 단상 구역 · 저장 전');

      pressUndo();
      expect(nameInput().value).toBe('단상 구역');
      expect(optionTexts()).toContain('z-02 · 단상 구역');
    });

    it('끄는 중 Esc 로 시작 상자로 돌아오면 그 끌기는 되돌릴 단계가 아니다', async () => {
      await renderPage();
      pick('z-02');
      await screen.findByLabelText('높이(m)');
      rename('다른 이름');
      dragTo({ min: [2, 6.2, 0], max: [5, 8.4, 0.4] });
      dragTo(Z02_BOX);
      releasePointer();
      expect(useZoneEditStore.getState().history['z-02']).toHaveLength(1);

      pressUndo();
      expect(nameInput().value).toBe('단상 구역');
      expect(optionTexts()).toContain('z-02 · 단상 구역');
    });

    it('끄는 중 다른 고침이 단계를 더했으면 시작 상자로 돌아와도 그 단계를 버리지 않는다', async () => {
      await renderPage();
      pick('z-02');
      await screen.findByLabelText('높이(m)');
      pressPointer();
      dragTo({ min: [2, 6.2, 0], max: [5, 8.4, 0.4] });
      rename('다른 이름');
      dragTo(Z02_BOX);
      releasePointer();
      // 끌기 단계(내용 없음)와 이름 단계. 이름 단계가 끌기 것으로 오해돼 사라지면 1 이다.
      expect(useZoneEditStore.getState().history['z-02']).toHaveLength(2);

      pressUndo();
      pressUndo();
      expect(nameInput().value).toBe('단상 구역');
      expect(pressUndo()).toBe(true);
    });

    it('상한에 닿은 기록에서도 끄는 중 쌓인 다른 단계를 버리지 않는다', async () => {
      await renderPage();
      pick('z-02');
      await screen.findByLabelText('높이(m)');
      for (let i = 0; i < HISTORY_LIMIT; i++) {
        rename(`이름 ${i}`);
        fireEvent.change(severitySelect(), { target: { value: i % 2 ? 'INFO' : 'CAUTION' } });
      }
      pressPointer();
      dragTo({ min: [2, 6.2, 0], max: [5, 8.4, 0.4] });
      rename('끄는 중 이름');
      dragTo(Z02_BOX);
      releasePointer();

      // 기록이 가득 차 있어도 새 단계는 가장 오래된 것을 밀어낼 뿐이라 개수로는 끌기 뒤에 단계가 더해졌는지 알 수 없다.
      expect(useZoneEditStore.getState().history['z-02']).toHaveLength(HISTORY_LIMIT);
      pressUndo();
      expect(useBoxEditStore.getState().boxEdit?.box).toEqual({
        min: [2, 6.2, 0],
        max: [5, 8.4, 0.4],
      });
      expect(nameInput().value).toBe(`이름 ${HISTORY_LIMIT - 1}`);
    });

    it('포인터 놓음을 받지 못한 끌기는 다음 누름에서 닫혀 다음 끌기와 한 단계로 합쳐지지 않는다', async () => {
      await renderPage();
      pick('z-02');
      await screen.findByLabelText('높이(m)');
      pressPointer();
      dragTo({ min: [2, 6.2, 0], max: [5, 8.4, 0.4] });
      // 창 밖에서 놓아 pointerup 이 오지 않았다.
      pressPointer();
      dragTo({ min: [3, 6.2, 0], max: [6, 8.4, 0.4] });
      releasePointer();
      expect(useZoneEditStore.getState().history['z-02']).toHaveLength(2);

      pressUndo();
      expect(useBoxEditStore.getState().boxEdit?.box).toEqual({
        min: [2, 6.2, 0],
        max: [5, 8.4, 0.4],
      });
      pressUndo();
      expect(useBoxEditStore.getState().boxEdit?.box).toEqual(Z02_BOX);
    });

    it('끌기를 이어서 두 번 하면 두 단계다', async () => {
      await renderPage();
      pick('z-02');
      await screen.findByLabelText('높이(m)');
      dragTo({ min: [2, 6.2, 0], max: [5, 8.4, 0.4] });
      releasePointer();
      dragTo({ min: [3, 6.2, 0], max: [6, 8.4, 0.4] });
      releasePointer();

      pressUndo();
      expect(useBoxEditStore.getState().boxEdit?.box).toEqual({
        min: [2, 6.2, 0],
        max: [5, 8.4, 0.4],
      });
      pressUndo();
      expect(useBoxEditStore.getState().boxEdit?.box).toEqual(Z02_BOX);
      expect(optionTexts()).toContain('z-02 · 단상 구역');
    });

    it('작업면 높이를 고친 뒤 포커스를 잃어도 같은 값은 단계를 더하지 않는다', async () => {
      await renderPage();
      pick('z-02');
      const height = await screen.findByLabelText('높이(m)');
      fireEvent.change(height, { target: { value: '0.6' } });
      fireEvent.keyDown(height, { key: 'Enter' });
      fireEvent.blur(height);
      expect(useZoneEditStore.getState().history['z-02']).toHaveLength(1);

      pressUndo();
      expect(screen.getByLabelText('높이(m)')).toHaveProperty('value', '0.40');
    });

    it('작업면 추가를 되돌리면 작업면 손잡이 모드도 풀린다', async () => {
      await renderPage();
      pick('z-04');
      await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
      act(() => button('+ 고소 작업면 추가').click());
      expect(useBoxEditStore.getState().boxEdit?.kind).toBe('surface');

      pressUndo();
      expect(screen.queryByLabelText('높이(m)')).toBeNull();
      expect(button('+ 고소 작업면 추가')).toBeTruthy();
      expect(useBoxEditStore.getState().boxEdit).toMatchObject({ id: 'z-04', kind: 'zone' });
      // 작업면 모드가 남아 있지 않아서 다시 추가하면 손잡이가 작업면으로 간다.
      act(() => button('+ 고소 작업면 추가').click());
      expect(useBoxEditStore.getState().boxEdit?.kind).toBe('surface');
    });

    it('이름·심각도·높이 입력칸 안의 Ctrl+Z 는 초안을 건드리지 않고 브라우저 기본 동작에 맡긴다', async () => {
      await renderPage();
      pick('z-02');
      await screen.findByLabelText('높이(m)');
      rename('다른 이름');
      fireEvent.change(severitySelect(), { target: { value: 'INFO' } });

      expect(pressUndo(nameInput())).toBe(true);
      expect(pressUndo(severitySelect())).toBe(true);
      expect(pressUndo(screen.getByLabelText('높이(m)'))).toBe(true);
      expect(nameInput().value).toBe('다른 이름');
      expect(severitySelect().value).toBe('INFO');
      expect(useZoneEditStore.getState().history['z-02']).toHaveLength(2);

      // 버튼에 포커스가 있을 때는 되돌린다.
      pressUndo(button('되돌리기'));
      expect(severitySelect().value).not.toBe('INFO');
    });

    it('한글 조합 중이거나 시프트·알트와 함께 누른 키는 무시하고, 한글 자판이어도 자판 위치로 되돌린다', async () => {
      await renderPage();
      pick('z-04');
      await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
      rename('다른 이름');

      pressUndo(window, { isComposing: true });
      pressUndo(window, { keyCode: 229 });
      pressUndo(window, { shiftKey: true });
      pressUndo(window, { altKey: true });
      pressUndo(window, { ctrlKey: false });
      expect(nameInput().value).toBe('다른 이름');

      pressUndo(window, { key: 'ㅋ' });
      expect(nameInput().value).not.toBe('다른 이름');
    });

    it('저장한 뒤에는 저장 전 값으로 되돌리지 않는다', async () => {
      await renderPage();
      pick('z-04');
      await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
      rename('개구부 B');
      expect(useZoneEditStore.getState().history['z-04']).toHaveLength(1);
      act(() => button('저장').click());
      await waitFor(() => expect(optionTexts()).toContain('z-04 · 개구부 B'));
      expect(writes()).toEqual([COMMIT]);
      // 저장한 값이 새 기준이라 초안과 되돌리기 기록이 모두 사라졌다.
      expect(useZoneEditStore.getState().drafts['z-04']).toBeUndefined();
      expect(useZoneEditStore.getState().history['z-04']).toBeUndefined();

      expect(pressUndo()).toBe(true);
      expect(nameInput().value).toBe('개구부 B');
      expect(optionTexts()).toContain('z-04 · 개구부 B');
    });

    it('되돌리기 버튼·구역 추가 뒤에는 그 구역의 기록이 없다', async () => {
      await renderPage();
      pick('z-02');
      await screen.findByLabelText('높이(m)');
      rename('다른 이름');
      act(() => button('되돌리기').click());
      expect(useZoneEditStore.getState().history['z-02']).toBeUndefined();

      pick('__new__');
      rename('새로 만든 구역');
      act(() => button('구역 추가').click());
      await waitFor(() => expect(picker().value).toBe('z-00000001'));
      expect(writes()).toEqual([COMMIT]);
      expect(useZoneEditStore.getState().history[NEW_KEY]).toBeUndefined();
      expect(useZoneEditStore.getState().drafts[NEW_KEY]).toBeUndefined();
    });

    it('구역을 바꿔도 구역마다 기록이 남아 각자 되돌린다', async () => {
      await renderPage();
      pick('z-04');
      await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
      rename('개구부 B');
      pick('z-02');
      await screen.findByLabelText('높이(m)');
      rename('단상 B');

      pressUndo();
      expect(nameInput().value).toBe('단상 구역');
      pick('z-04');
      await waitFor(() => expect(nameInput().value).toBe('개구부 B'));
      pressUndo();
      expect(nameInput().value).toBe('개구부 구역');
    });

    it('관리자가 아니면 Ctrl+Z 를 무시한다', async () => {
      const store = useZoneEditStore.getState();
      store.record('z-02');
      store.update('z-02', () => ({ patch: { name: '남긴 초안' }, box: null }));
      signInAs('VIEWER');
      await renderPage();
      pick('z-02');
      await screen.findByLabelText('높이(m)');

      expect(pressUndo()).toBe(true);
      expect(useZoneEditStore.getState().drafts['z-02']).toBeDefined();
      expect(useZoneEditStore.getState().history['z-02']).toHaveLength(1);
    });
  });
});
