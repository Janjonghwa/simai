import type { ViewerEditBox } from '@/features/viewer3d';
import { ARM_MS } from '@/features/zones/useArmedDelete';
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
let zonesMock: ZonesMock;

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
  zonesMock = createZonesMock({ staticObjects: staticObjects.current });
  server.resetHandlers(...zonesMock.handlers, ...staticObjects.handlers);
  log.splice(0, log.length);
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

async function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <ZonesPage />
    </QueryClientProvider>,
  );
  await screen.findByRole('option', { name: /z-04/ });
  // 작업면 목록과 구성 묶음 목록이 도착해야 이은 작업면·발행 전이 맞다.
  await waitFor(() => expect(client.getQueryData(['static-objects'])).toBeDefined());
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

  it('새 구역은 OPENING·VOLUME_3D 로 만들고 저장과 발행을 한 번에 보낸다', async () => {
    await renderPage();
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
      target: { value: '새로 만든 구역' },
    });
    fireEvent.change(screen.getByRole('combobox', { name: '심각도' }), {
      target: { value: 'CAUTION' },
    });
    act(() => button('구역 추가').click());
    await waitFor(() => expect(picker().value).toBe('z-00000001'));
    await waitFor(() => expect(writes()).toEqual(['POST /api/zones', 'POST /api/config-bundles']));
    const created = (await (
      await fetch('http://api.test/api/zones', {
        headers: { Authorization: 'Bearer mock-token.admin' },
      })
    ).json()) as Record<string, unknown>[];
    expect(created.find((z) => z.zone_id === 'z-00000001')).toMatchObject({
      name: '새로 만든 구역',
      zone_kind: 'OPENING',
      eval_type: 'VOLUME_3D',
      severity: 'CAUTION',
      z_min_m: 0,
      z_max_m: 2,
    });
    // 만든 구역은 바로 고를 수 있고 발행 전이 아니다.
    expect(optionTexts()).toContain('z-00000001 · 새로 만든 구역');
  });

  it('새 구역에 작업면을 더하면 구역 → 작업면 → 발행 순서로 보낸다', async () => {
    await renderPage();
    act(() => button('+ 고소 작업면 추가').click());
    expect(screen.getByLabelText('높이(m)')).toHaveProperty('value', '0.40');
    act(() => button('구역 추가').click());
    await waitFor(() => expect(picker().value).toBe('z-00000001'));
    await waitFor(() =>
      expect(writes()).toEqual([
        'POST /api/zones',
        'POST /api/static-objects',
        'POST /api/config-bundles',
      ]),
    );
    // 만든 작업면은 구역 안에 있어 새 구역의 작업면 카드로 이어진다.
    await waitFor(() => expect(screen.getByLabelText('높이(m)')).toHaveProperty('value', '0.40'));
  });

  it('이은 작업면(중심이 구역 바닥 안)이 카드에 보이고, 3D 손잡이는 구역·작업면을 전환해 고친다', async () => {
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

  it('구역을 줄여 작업면 중심이 밖이 되면 알리고 저장을 막는다', async () => {
    await renderPage();
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    // 폭을 1 m 로 줄이면 작업면 중심(X 2.5)이 구역(X 1~2) 밖에 남는다.
    act(() => useBoxEditStore.getState().onChange({ min: [1, 6.2, 0], max: [2, 8.4, 0.4] }));
    expect(screen.getByText('작업면 중심이 구역 안에 있어야 합니다.')).toBeTruthy();
    expect((button('저장') as HTMLButtonElement).disabled).toBe(true);
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

  it('저장 순서는 구역 PUT → 작업면 PUT → 발행이고, 저장하면 저장 전·발행 전이 사라진다', async () => {
    await renderPage();
    pick('z-02');
    await screen.findByLabelText('높이(m)');
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
      target: { value: '단상 B' },
    });
    const height = screen.getByLabelText('높이(m)');
    fireEvent.change(height, { target: { value: '0.6' } });
    fireEvent.keyDown(height, { key: 'Enter' });
    act(() => button('저장').click());
    await waitFor(() => expect(writes()).toHaveLength(3));
    expect(writes()).toEqual([
      'PUT /api/zones/z-02',
      'PUT /api/static-objects/ws-0a0c0001',
      'POST /api/config-bundles',
    ]);
    await waitFor(() => expect(optionTexts()).toContain('z-02 · 단상 B'));
    expect(screen.queryByRole('button', { name: '다시 보내기' })).toBeNull();
  });

  it('작업면만 고치면 구역은 새 버전을 만들지 않는다', async () => {
    await renderPage();
    pick('z-02');
    const height = await screen.findByLabelText('높이(m)');
    fireEvent.change(height, { target: { value: '0.5' } });
    fireEvent.keyDown(height, { key: 'Enter' });
    act(() => button('저장').click());
    await waitFor(() => expect(writes()).toHaveLength(2));
    expect(writes()).toEqual(['PUT /api/static-objects/ws-0a0c0001', 'POST /api/config-bundles']);
  });

  it('다른 관리자가 먼저 저장해 409(CONCURRENT_EDIT)면 최신 구역을 다시 받고, 초안은 남기고, 발행하지 않는다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    server.use(
      http.put('*/api/zones/:id', () =>
        HttpResponse.json({ status: 409, detail: 'x', code: 'CONCURRENT_EDIT' }, { status: 409 }),
      ),
    );
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
      target: { value: '내 이름' },
    });
    log.splice(0, log.length);
    act(() => button('저장').click());
    expect(
      await screen.findByText(
        '그 사이 다른 버전이 저장되어 최신 구역을 다시 받았습니다. 확인한 뒤 다시 저장하세요.',
      ),
    ).toBeTruthy();
    // 저장 전 확인(GET) → PUT(409) → 항목을 다시 받는다(GET). 발행은 보내지 않는다.
    await waitFor(() =>
      expect(log.filter((e) => e === 'GET /api/zones').length).toBeGreaterThanOrEqual(2),
    );
    expect(log).toContain('PUT /api/zones/z-04');
    expect(log).not.toContain('POST /api/config-bundles');
    expect(optionTexts()).toContain('z-04 · 개구부 구역 · 저장 전');
    expect(screen.getByRole('textbox', { name: '이름' })).toHaveProperty('value', '내 이름');
  });

  it('발행이 실패하면 한 줄과 다시 보내기가 나오고, 발행 전은 묶음 items 로 센다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    zonesMock.failNextPublish('CALIBRATION_MISSING');
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
      target: { value: '개구부 B' },
    });
    act(() => button('저장').click());
    const alert = await screen.findByText('저장했지만 현장 판정으로 보내지 못했습니다.');
    expect(alert).toBeTruthy();
    expect(screen.getByText('보정 문서가 없어 현장에 반영할 수 없습니다.')).toBeTruthy();
    // 새 버전(v2)은 마지막 묶음(cfg-12)의 items 에 없어 발행 전이다.
    await waitFor(() => expect(optionTexts()).toContain('z-04 · 개구부 B · 발행 전'));
    log.splice(0, log.length);
    act(() => button('다시 보내기').click());
    await waitFor(() =>
      expect(screen.queryByText('저장했지만 현장 판정으로 보내지 못했습니다.')).toBeNull(),
    );
    await waitFor(() => expect(writes()).toEqual(['POST /api/config-bundles']));
    await waitFor(() => expect(optionTexts()).toContain('z-04 · 개구부 B'));
  });

  it(
    '삭제는 두 번 눌러야 한다: 첫 클릭은 「삭제 확인」(3초 뒤 되돌아간다), 두 번째가 구역 → 이은 작업면 보관 → 발행',
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
      await waitFor(() => expect(writes()).toHaveLength(3));
      expect(writes()).toEqual([
        'DELETE /api/zones/z-02',
        'DELETE /api/static-objects/ws-0a0c0001',
        'POST /api/config-bundles',
      ]);
      await waitFor(() => expect(optionTexts()).toEqual(['새 구역 추가', 'z-04 · 개구부 구역']));
      // 삭제한 뒤에는 새 구역 양식으로 돌아간다.
      expect(picker().value).toBe('__new__');
    },
    ARM_MS + 8000,
  );

  it('작업면이 없는 구역을 삭제하면 구역 보관과 발행만 보낸다', async () => {
    await renderPage();
    pick('z-04');
    await screen.findByRole('button', { name: '+ 고소 작업면 추가' });
    const deleteButton = () => button(/^삭제( 확인)?$/);
    act(() => deleteButton().click());
    act(() => deleteButton().click());
    await waitFor(() => expect(writes()).toHaveLength(2));
    expect(writes()).toEqual(['DELETE /api/zones/z-04', 'POST /api/config-bundles']);
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
      act(() => button('저장').click());
      await waitFor(() => expect(optionTexts()).toContain('z-04 · 개구부 B'));
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
      expect(useZoneEditStore.getState().history['__new__']).toBeUndefined();
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
