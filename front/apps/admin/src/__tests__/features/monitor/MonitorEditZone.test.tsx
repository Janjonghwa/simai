import { SpatialViewportHost } from '@/features/layout/SpatialViewportHost';
import { MonitorPage } from '@/features/monitor/MonitorPage';
import type {
  Viewer3DHandle,
  Viewer3DProps,
  ViewerEditBox,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore, type Role } from '@/store/session.store';
import { useZoneDraftsStore } from '@/store/zoneDrafts.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
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
  onEditPreview: null as Viewer3DProps['onEditPreview'] | null,
  onEditResult: null as Viewer3DProps['onEditResult'] | null,
  overlay: null as Viewer3DProps['editDraftOverlay'] | null,
  draft: { min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] } as ViewerEditBox,
}));

function FakeViewer({
  ref,
  onSelectionChange,
  onStatusChange,
  onEditPreview,
  onEditResult,
  editDraftOverlay,
}: Viewer3DProps) {
  useEffect(() => {
    fake.onSelect = onSelectionChange ?? null;
    fake.onEditPreview = onEditPreview ?? null;
    fake.onEditResult = onEditResult ?? null;
    fake.overlay = editDraftOverlay ?? null;
  }, [onSelectionChange, onEditPreview, onEditResult, editDraftOverlay]);

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
        beginEdit: (target: ViewerTarget) => {
          fake.draft = { min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] };
          fake.onEditPreview?.({
            target,
            mode: 'LIVE',
            before: { min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] },
            draft: fake.draft,
            provenance: {
              sourceId: 's-1',
              zoneContract: 'DEMO_UNCONFIRMED',
              zonesReceivedAt: 1000,
              sceneVersion: 'scene-3',
              configVersion: null,
            },
            saved: false,
          });
          return { ok: true };
        },
        updateEditField: ({ field, value }) => {
          if (field === 'originX') {
            const w = fake.draft.max[0] - fake.draft.min[0];
            fake.draft = {
              min: [value, fake.draft.min[1], fake.draft.min[2]],
              max: [value + w, fake.draft.max[1], fake.draft.max[2]],
            };
          }
          fake.onEditPreview?.({
            target: { type: 'zone', id: 'z-04' },
            mode: 'LIVE',
            before: { min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] },
            draft: fake.draft,
            provenance: {
              sourceId: 's-1',
              zoneContract: 'DEMO_UNCONFIRMED',
              zonesReceivedAt: 1000,
              sceneVersion: 'scene-3',
              configVersion: null,
            },
            saved: false,
          });
          return { ok: true };
        },
        finishEdit: () => {
          const res = {
            status: 'FINISHED' as const,
            target: { type: 'zone' as const, id: 'z-04' },
            mode: 'LIVE' as const,
            before: {
              min: [5.8, 1.2, 0] as [number, number, number],
              max: [8.6, 3.8, 2.4] as [number, number, number],
            },
            after: fake.draft,
            changed: true,
            provenance: {
              sourceId: 's-1',
              zoneContract: 'DEMO_UNCONFIRMED' as const,
              zonesReceivedAt: 1000,
              sceneVersion: 'scene-3',
              configVersion: null,
            },
            saved: false as const,
          };
          fake.onEditResult?.(res);
          return res;
        },
        cancelEdit: () => null,
      }) as Viewer3DHandle,
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
    useZoneDraftsStore.getState().clear();
    useRealtimeStore.setState(
      { ...useRealtimeStore.getInitialState(), source: { id: 's-1', pushZones: vi.fn() } as never },
      true,
    );
    fake.mounts = 0;
    fake.unmounts = 0;
    fake.onSelect = null;
    fake.overlay = null;
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

    // /zones 에서 편집 중 값을 바꾼 뒤 관제(/)로 이동하면 손잡이는 닫히고 변경분은 Ghost 로 유지된다.
    act(() => screen.getByRole('button', { name: 'go-zones' }).click());
    act(() => screen.getByRole('button', { name: '구역 수정' }).click());
    act(() => screen.getByRole('button', { name: /정밀 입력/ }).click());
    const xInput = screen.getByLabelText('시작 X (m)') as HTMLInputElement;
    fireEvent.focus(xInput);
    fireEvent.change(xInput, { target: { value: '6.5' } });
    fireEvent.keyDown(xInput, { key: 'Enter' });

    act(() => screen.getByRole('button', { name: 'go-monitor' }).click());
    expect(fake.overlay).toBeNull();

    act(() => screen.getByRole('button', { name: 'go-zones' }).click());
    expect(fake.overlay).toEqual({
      target: { type: 'zone', id: 'z-04' },
      box: { min: [6.5, 1.2, 0], max: [9.3, 3.8, 2.4] },
    });
    // 구역 편집 화면을 10번 다시 그리는 시험이라, 전체 시험을 함께 돌리는 부하에서는 기본 5초가 빠듯하다.
  }, 15_000);
});
