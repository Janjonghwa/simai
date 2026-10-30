import { SpatialViewportHost } from '@/features/layout/SpatialViewportHost';
import { MonitorPage } from '@/features/monitor/MonitorPage';
import type {
  Viewer3DHandle,
  Viewer3DProps,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { useBoxEditStore } from '@/store/boxEdit.store';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore, type Role } from '@/store/session.store';
import { useZoneEditStore } from '@/store/zoneEdit.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { useEffect, useImperativeHandle, useRef, useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ZONES_RAW = {
  contract: 'DEMO_UNCONFIRMED' as const,
  zones: [
    {
      zone_id: 'z-04',
      name: '북동 위험 구역',
      aabb: { min_xyz: [5.8, 1.2, 0], max_xyz: [8.6, 3.8, 2.4] },
    },
  ],
};

vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: [], loaded: true, failed: false }),
}));
vi.mock('@/features/zones/useZones', async () => {
  const { zoneOf } = await import('@/__tests__/features/zones/zoneFixture');
  return {
    useZones: () => ({
      zones: [
        zoneOf({
          zoneId: 'z-04',
          name: '북동 위험 구역',
          min: [5.8, 1.2, 0],
          max: [8.6, 3.8, 2.4],
        }),
      ],
      raw: ZONES_RAW,
      failed: false,
    }),
  };
});
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));

const fake = vi.hoisted(() => ({
  mounts: 0,
  unmounts: 0,
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
    fake.mounts += 1;
    if (!reported.current) {
      reported.current = true;
      onStatusChange?.({ phase: 'ready', mode: 'LIVE', error: null } as ViewerStatus);
    }
    return () => {
      fake.unmounts += 1;
    };
  }, [onStatusChange]);

  return <div data-testid="viewer3d" />;
}

const loadFake = () => Promise.resolve({ default: FakeViewer });

function signInAs(role: Role) {
  useSessionStore.setState({
    session: { accessToken: `token-${role}`, user: { id: role.toLowerCase(), role } },
  });
}

describe('MonitorPage 구역 선택 수정 동작 및 단일 Canvas 공유', () => {
  beforeEach(() => {
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
    fake.mounts = 0;
    fake.unmounts = 0;
    fake.onSelect = null;
  });
  afterEach(cleanup);

  it('관리자(ADMIN)가 구역을 고르면 구역 수정 버튼이 뜨고, 사람 선택·미선택·비관리자(FOREMAN/VIEWER)에서는 뜨지 않는다(R05)', async () => {
    const onEditZone = vi.fn();
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <SpatialViewportHost loadViewer={loadFake}>
          <MonitorPage onEditZone={onEditZone} />
        </SpatialViewportHost>
      </QueryClientProvider>,
    );
    await screen.findByTestId('viewer3d');
    expect(screen.queryByRole('button', { name: /구역 수정/ })).toBeNull();

    act(() => fake.onSelect?.({ type: 'person', id: 'w-0012' }));
    expect(screen.queryByRole('button', { name: /구역 수정/ })).toBeNull();

    act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
    const btn = screen.getByRole('button', { name: '구역 수정 z-04' });
    act(() => btn.click());
    expect(onEditZone).toHaveBeenCalledWith('z-04');

    act(() => signInAs('VIEWER'));
    expect(screen.queryByRole('button', { name: /구역 수정/ })).toBeNull();
    act(() => signInAs('FOREMAN'));
    expect(screen.queryByRole('button', { name: /구역 수정/ })).toBeNull();
  });

  it('관제(/)와 구역 편집(/zones)을 10회 왕복해도 단일 Viewer3D 를 재사용하고, 관제(/)에서는 Ghost 와 Fade 를 숨긴다(R04, U03, U11)', async () => {
    const qc = new QueryClient();
    function Harness() {
      const [route, setRoute] = useState<'monitor' | 'zones'>('monitor');
      return (
        <QueryClientProvider client={qc}>
          <button type="button" onClick={() => setRoute('monitor')}>
            go-monitor
          </button>
          <button type="button" onClick={() => setRoute('zones')}>
            go-zones
          </button>
          <SpatialViewportHost loadViewer={loadFake} zoneEditor={route === 'zones'}>
            {route === 'monitor' ? (
              <MonitorPage onEditZone={() => setRoute('zones')} />
            ) : (
              <ZonesPage />
            )}
          </SpatialViewportHost>
        </QueryClientProvider>
      );
    }

    render(<Harness />);
    await screen.findByTestId('viewer3d');
    expect(fake.mounts).toBe(1);
    expect(screen.queryByTestId('zone-edit-fade')).toBeNull();

    act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
    for (let i = 0; i < 10; i += 1) {
      act(() => screen.getByRole('button', { name: 'go-zones' }).click());
      expect(screen.getByTestId('zone-edit-fade')).toBeTruthy();
      act(() => screen.getByRole('button', { name: 'go-monitor' }).click());
      expect(screen.queryByTestId('zone-edit-fade')).toBeNull();
    }
    expect(fake.mounts).toBe(1);
    expect(fake.unmounts).toBe(0);
    // 왕복 후에도 z-04 선택이 그대로 유지된다.
    expect(screen.getByRole('button', { name: '구역 수정 z-04' })).toBeTruthy();

    // /zones 에서 3D 손잡이로 구역 상자를 고친 뒤 관제(/)로 이동하면 손잡이는 내려가고, 돌아오면 고친 초안이 그대로다.
    act(() => screen.getByRole('button', { name: 'go-zones' }).click());
    await waitFor(() => expect(useBoxEditStore.getState().boxEdit?.id).toBe('z-04'));
    act(() => useBoxEditStore.getState().onChange({ min: [6.5, 1.2, 0], max: [9.3, 3.8, 2.4] }));

    act(() => screen.getByRole('button', { name: 'go-monitor' }).click());
    expect(useBoxEditStore.getState().boxEdit).toBeNull();

    act(() => screen.getByRole('button', { name: 'go-zones' }).click());
    await waitFor(() =>
      expect(useBoxEditStore.getState().boxEdit).toMatchObject({
        id: 'z-04',
        kind: 'zone',
        box: { min: [6.5, 1.2, 0], max: [9.3, 3.8, 2.4] },
      }),
    );
    expect(screen.getByRole('option', { name: 'z-04 · 북동 위험 구역 · 저장 전' })).toBeTruthy();
    // 구역 편집 화면을 10번 다시 그리는 시험이라, 전체 시험을 함께 돌리는 부하에서는 기본 5초가 빠듯하다.
  }, 15_000);
});
