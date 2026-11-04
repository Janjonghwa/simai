import { ALARM_TEXT } from '@/features/events/alarmTone';
import { Dock } from '@/features/layout/Dock';
import { SpatialViewportHost } from '@/features/layout/SpatialViewportHost';
import { MonitorPage } from '@/features/monitor/MonitorPage';
import { severityTone } from '@/features/plan/plan';
import { SetupPage } from '@/features/setup/SetupPage';
import type {
  Viewer3DHandle,
  Viewer3DProps,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d';
import { PALETTE } from '@/features/viewer3d/palette';
import { zoneStyle } from '@/features/viewer3d/zones/zonePresentation';
import { SEVERITY_CHOICES, type ZoneCommitRequest } from '@/features/zones/types';
import { handlers } from '@/mocks/handlers';
import { createStaticObjectsMock } from '@/mocks/staticObjects';
import { createZonesMock } from '@/mocks/zones';
import { Route as IndexRoute } from '@/routes/_app/index';
import { useBoxEditStore } from '@/store/boxEdit.store';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore, type Role } from '@/store/session.store';
import { useZoneEditStore } from '@/store/zoneEdit.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http } from 'msw/http';
import { setupServer } from 'msw/node';
import { useEffect, useImperativeHandle, useRef, type ReactNode } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const navigateMock = vi.hoisted(() => vi.fn());

vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: [], loaded: true, failed: false }),
}));
vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return {
    api: createHttp({
      baseUrl: 'https://monitor-edit.invalid',
      getToken: () => 'mock-token.admin',
    }),
  };
});
vi.mock('@tanstack/react-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-router')>();
  return {
    ...actual,
    useNavigate: () => navigateMock,
    Link: ({
      to,
      children,
      className,
      'aria-label': ariaLabel,
    }: {
      to: string;
      children?: ReactNode;
      className?: string;
      'aria-label'?: string;
    }) => (
      <a href={to} className={className} aria-label={ariaLabel}>
        {children}
      </a>
    ),
  };
});

const fake = vi.hoisted(() => ({
  onSelect: null as ((target: ViewerTarget | null) => void) | null,
}));

function FakeViewer({ ref, onSelectionChange, onStatusChange }: Viewer3DProps) {
  useEffect(() => {
    fake.onSelect = onSelectionChange ?? null;
  }, [onSelectionChange]);

  useImperativeHandle(
    ref,
    () =>
      ({
        select: (target: ViewerTarget) => {
          fake.onSelect?.(target);
          return { ok: true };
        },
        focus: () => ({ ok: true }),
        clearSelection: () => {
          fake.onSelect?.(null);
          return { ok: true };
        },
        setMode: () => ({ ok: true }),
      }) as unknown as Viewer3DHandle,
  );

  const reported = useRef(false);
  useEffect(() => {
    if (!reported.current) {
      reported.current = true;
      onStatusChange?.({ phase: 'ready', mode: 'LIVE', error: null } as ViewerStatus);
    }
  }, [onStatusChange]);

  return <div data-testid="viewer3d" />;
}

const loadFake = () => Promise.resolve({ default: FakeViewer });

const server = setupServer();
const commits: ZoneCommitRequest[] = [];

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

function signInAs(role: Role) {
  useSessionStore.setState({
    session: { accessToken: 'mock-token.admin', user: { id: role.toLowerCase(), role } },
  });
}

function renderMonitor(initialEdit = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SpatialViewportHost loadViewer={loadFake}>
        <MonitorPage initialEdit={initialEdit} />
      </SpatialViewportHost>
    </QueryClientProvider>,
  );
}

describe('관제 관리자 편집 모드', () => {
  beforeEach(() => {
    const staticObjects = createStaticObjectsMock();
    const zonesMock = createZonesMock({ staticObjects: staticObjects.store });
    commits.splice(0, commits.length);
    navigateMock.mockReset();
    server.resetHandlers(
      http.post('*/api/zones/commit', async ({ request }) => {
        commits.push((await request.clone().json()) as ZoneCommitRequest);
        return undefined;
      }),
      ...zonesMock.handlers,
      ...staticObjects.handlers,
      ...handlers,
    );
    signInAs('ADMIN');
    useZoneEditStore.getState().clear();
    useBoxEditStore.getState().clear();
    useRealtimeStore.setState(
      {
        ...useRealtimeStore.getInitialState(),
        source: { id: 's-1', pushZones: vi.fn(), pushWorkSurfaces: vi.fn() } as never,
      },
      true,
    );
  });
  afterEach(cleanup);

  it('관리자(ADMIN)만 편집 모드를 열고 취소할 수 있으며, 편집 중 표시와 기즈모·초안 정리가 동작한다', async () => {
    renderMonitor();
    await screen.findByTestId('viewer3d');
    expect(screen.queryByRole('region', { name: '구역 편집 도구' })).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();

    act(() => screen.getByRole('button', { name: '관리자 편집 모드' }).click());
    expect(screen.getByRole('region', { name: '구역 편집 도구' })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe('편집 중');
    await waitFor(() => expect(useBoxEditStore.getState().boxEdit?.kind).toBe('zone'));

    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
      target: { value: '임시 초안' },
    });
    expect(Object.keys(useZoneEditStore.getState().drafts).length).toBeGreaterThan(0);

    act(() => screen.getByRole('button', { name: '편집 취소' }).click());
    expect(screen.queryByRole('region', { name: '구역 편집 도구' })).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    expect(useZoneEditStore.getState().drafts).toEqual({});
    expect(useBoxEditStore.getState().boxEdit).toBeNull();

    for (const role of ['VIEWER', 'FOREMAN'] as const) {
      act(() => signInAs(role));
      expect(screen.queryByRole('button', { name: '관리자 편집 모드' })).toBeNull();
      expect(screen.queryByRole('region', { name: '구역 편집 도구' })).toBeNull();
    }
  });

  it('구역 등급 선택지는 CRITICAL(빨강)·HIGH(주황) 2종뿐이고 이벤트 CAUTION 표시는 유지된다', async () => {
    renderMonitor(true);
    await screen.findByTestId('viewer3d');
    const severity = screen.getByRole('combobox', { name: '심각도' });
    const options = within(severity).getAllByRole('option') as HTMLOptionElement[];
    expect(SEVERITY_CHOICES).toEqual(['CRITICAL', 'HIGH']);
    expect(options.map((o) => [o.value, o.textContent])).toEqual([
      ['CRITICAL', '위험'],
      ['HIGH', '경고'],
    ]);
    expect(zoneStyle('UNVERIFIED', false, PALETTE.dark, 'CRITICAL').color).toBe(
      PALETTE.dark.risk.CRITICAL.color,
    );
    expect(zoneStyle('UNVERIFIED', false, PALETTE.dark, 'HIGH').color).toBe(
      PALETTE.dark.risk.HIGH.color,
    );
    expect(ALARM_TEXT.CAUTION).toBe('text-sev-caution');
    expect(severityTone('CAUTION')).toBe('caution');
  });

  it('고소 작업면을 그리면 작업면 OBB 와 둘레 0.30 m HIGH 구역을 함께 만들어 POST /api/zones/commit 한 번으로 발행한다', async () => {
    renderMonitor(true);
    await screen.findByTestId('viewer3d');
    await screen.findByRole('option', { name: /z-04/ });

    act(() => screen.getByRole('button', { name: '+ 고소 작업면 추가' }).click());
    const severity = screen.getByRole('combobox', { name: '심각도' }) as HTMLSelectElement;
    expect(severity.value).toBe('HIGH');
    expect(severity.disabled).toBe(true);
    expect(useBoxEditStore.getState().boxEdit?.kind).toBe('surface');

    act(() =>
      useBoxEditStore.getState().onChange({
        min: [4.0, 3.0, 0],
        max: [6.0, 5.0, 1.2],
      }),
    );

    const addBtn = screen.getByRole('button', { name: '구역 추가' }) as HTMLButtonElement;
    await waitFor(() => expect(addBtn.disabled).toBe(false));
    act(() => addBtn.click());

    await waitFor(() => expect(commits).toHaveLength(1));
    expect(commits[0]).toMatchObject({
      zone: {
        op: 'CREATE',
        body: {
          severity: 'HIGH',
          polygon_xy: [
            [3.7, 2.7],
            [6.3, 2.7],
            [6.3, 5.3],
            [3.7, 5.3],
          ],
          z_min_m: 0,
          z_max_m: 2,
        },
      },
      surface: {
        op: 'CREATE',
        body: {
          kind: 'WORK_SURFACE',
          top_height_m: 1.2,
          obb: {
            center: [5.0, 4.0, 0.6],
            size: [2.0, 2.0, 1.2],
            yaw_rad: 0,
          },
        },
      },
      publish: true,
    });
  });

  it('/zones 라우트 제거 뒤에도 도크 링크·세팅 구역 편집 이동·검색 파라미터가 깨지지 않는다', () => {
    render(<Dock />);
    const nav = screen.getByRole('navigation', { name: '주 메뉴' });
    const hrefs = within(nav)
      .getAllByRole('link')
      .map((a) => a.getAttribute('href'));
    expect(hrefs).toEqual(['/', '/cctv', '/tbm', '/history']);
    expect(hrefs).not.toContain('/zones');
    cleanup();

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <SetupPage />
      </QueryClientProvider>,
    );
    act(() => screen.getByRole('button', { name: /3완료/ }).click());
    act(() => screen.getByRole('button', { name: '구역 편집' }).click());
    expect(navigateMock).toHaveBeenCalledWith({ to: '/', search: { edit: true } });

    const validateSearch = IndexRoute.options.validateSearch;
    if (typeof validateSearch !== 'function') throw new Error('expected validateSearch function');
    expect(validateSearch({ edit: true })).toEqual({ edit: true });
    expect(validateSearch({ edit: 'true' })).toEqual({ edit: true });
    expect(validateSearch({ edit: '0' })).toEqual({
      edit: undefined,
    });
  });
});
