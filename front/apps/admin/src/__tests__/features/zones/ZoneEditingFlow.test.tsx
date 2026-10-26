import { SpatialViewportHost } from '@/features/layout/SpatialViewportHost';
import type { Viewer3DProps, ViewerEditOverlayInput } from '@/features/viewer3d';
import { createSpatialDataStore } from '@/features/viewer3d/data/spatialData.store';
import { createEditStore, previewOf } from '@/features/viewer3d/editing/editSession.store';
import { createViewerStore } from '@/features/viewer3d/viewer3d.store';
import { ZonesPage } from '@/features/zones/ZonesPage';
import type { ZoneBox, ZonesResponse } from '@/features/zones/types';
import { worldStateAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import { useZoneDraftsStore } from '@/store/zoneDrafts.store';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useEffect, useImperativeHandle, useRef, useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const input = vi.hoisted(() => ({
  zones: [] as ZoneBox[],
  raw: undefined as ZonesResponse | undefined,
  failed: false,
}));
vi.mock('@/features/zones/useZones', () => ({ useZones: () => input }));
vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: [], loaded: true, failed: false }),
}));
vi.mock('@/lib/api', () => ({ api: vi.fn() }));

let engine: ReturnType<typeof createEditStore>;
let viewer: ReturnType<typeof createViewerStore>;
let overlay: ViewerEditOverlayInput;

// WebGL만 제외하고 실제 Viewer 선택·편집 Store와 FE Host의 이벤트 순서를 연결한다.
function StoreViewer({ ref, ...props }: Viewer3DProps) {
  const callbacks = useRef(props);
  useEffect(() => {
    callbacks.current = props;
  });
  const [stores] = useState(() => {
    const v = createViewerStore();
    const data = createSpatialDataStore();
    data.getState().reset(useRealtimeStore.getState().source?.id ?? null);
    data.getState().ingestZones({ ok: true, raw: input.raw }, Date.now());
    const world = worldStateAt(Date.now());
    data.getState().ingest(world, Date.now());
    for (const zone of input.zones) v.getState().registerTarget({ type: 'zone', id: zone.zoneId });
    v.getState().registerTarget({ type: 'person', id: 'w-0015' });
    v.getState().markReady();
    return { v, data, edit: createEditStore(v, data) };
  });
  const raw = input.raw;
  useEffect(() => {
    stores.data.getState().ingestZones({ ok: true, raw }, Date.now());
  }, [stores, raw]);
  useEffect(() => {
    viewer = stores.v;
    engine = stores.edit;
    overlay = props.editDraftOverlay ?? null;
  });
  useImperativeHandle(
    ref,
    () => ({
      select: stores.v.getState().select,
      clearSelection: stores.v.getState().clearSelection,
      focus: stores.v.getState().focusOn,
      setMode: stores.v.getState().setMode,
      beginEdit: stores.edit.store.getState().begin,
      updateEditField: ({ field, value }) => stores.edit.store.getState().updateField(field, value),
      finishEdit: stores.edit.store.getState().finish,
      cancelEdit: () => stores.edit.store.getState().cancel('USER'),
    }),
    [stores],
  );
  useEffect(() => {
    const offViewer = stores.v.subscribe((state, prev) => {
      if (state.selected !== prev.selected) callbacks.current.onSelectionChange?.(state.selected);
      if (state.activated && state.activated !== prev.activated)
        callbacks.current.onTargetDoubleClick?.(state.activated.target);
    });
    const disconnect = stores.edit.connect();
    const offEdit = stores.edit.store.subscribe((state, prev) => {
      if (state.lastResult && state.lastResult !== prev.lastResult)
        callbacks.current.onEditResult?.(state.lastResult);
      const now = state.session;
      if (
        now &&
        !now.dragging &&
        (!prev.session || now.draft !== prev.session.draft || prev.session.dragging)
      ) {
        callbacks.current.onEditPreview?.(previewOf(now));
      }
    });
    callbacks.current.onStatusChange?.({ phase: 'ready', mode: 'LIVE', error: null });
    return () => {
      offViewer();
      disconnect();
      offEdit();
    };
  }, [stores]);
  return <div data-testid="store-viewer" />;
}
const load = () => Promise.resolve({ default: StoreViewer });

async function mount() {
  const result = render(
    <SpatialViewportHost loadViewer={load} zoneEditor>
      <ZonesPage />
    </SpatialViewportHost>,
  );
  await screen.findByTestId('store-viewer');
  return result;
}
function select(id: string) {
  act(() => {
    viewer.getState().select({ type: 'zone', id });
  });
}
function begin(id: string) {
  select(id);
  fireEvent.click(screen.getByRole('button', { name: /^(구역 수정|계속 편집)$/ }));
}
function field(label = '시작 X (m)') {
  if (!screen.queryByRole('group', { name: 'World 수치 입력' }))
    fireEvent.click(screen.getByRole('button', { name: /정밀 입력/ }));
  return screen.getByLabelText(label) as HTMLInputElement;
}
function enter(value: string, label?: string) {
  const element = field(label);
  fireEvent.focus(element);
  fireEvent.change(element, { target: { value } });
  fireEvent.keyDown(element, { key: 'Enter' });
}
const overlays = () => (overlay ? (Array.isArray(overlay) ? overlay : [overlay]) : []);

beforeEach(() => {
  useSessionStore.setState({
    session: { accessToken: 'test', user: { id: 'admin', role: 'ADMIN' } },
  });
  useRealtimeStore.setState(
    {
      ...useRealtimeStore.getInitialState(),
      source: { id: 's-1', pushZones: vi.fn() } as never,
      world: { payload: worldStateAt(Date.now()).payload, receivedAt: Date.now() },
    },
    true,
  );
  input.zones = [
    { zoneId: 'z-04', name: '구역 4', min: [5.8, 1.2, 0], max: [8.6, 3.8, 2.4] },
    { zoneId: 'z-02', name: '구역 2', min: [1, 6.2, 0], max: [4, 8.4, 0.4] },
  ];
  input.raw = {
    contract: 'DEMO_UNCONFIRMED',
    zones: input.zones.map((z) => ({
      zone_id: z.zoneId,
      name: z.name,
      aabb: { min_xyz: z.min, max_xyz: z.max },
    })),
  };
  overlay = null;
  useZoneDraftsStore.getState().clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('실제 Store 기반 구역 편집 회귀', () => {
  it('F01/F06: Enter로 보관한 구역의 입력이 다음 구역으로 넘어가지 않고 focus/blur는 형상을 바꾸지 않는다', async () => {
    await mount();
    begin('z-04');
    enter('8');
    expect(engine.store.getState().session).toBeNull();
    begin('z-02');
    const x = field();
    expect(x.value).toBe('1');
    fireEvent.focus(x);
    fireEvent.blur(x);
    expect(engine.store.getState().session!.draft.min[0]).toBe(1);
  });

  it.each(['z-02', 'person', 'empty'])(
    'F02/F05: %s 선택으로 세션이 실제 취소되어도 마지막 유효 초안은 보관한다',
    async (next) => {
      await mount();
      begin('z-04');
      act(() => {
        engine.store.getState().updateField('originX', 8);
      });
      act(() => {
        if (next === 'empty') viewer.getState().clearSelection();
        else
          viewer
            .getState()
            .select(
              next === 'person' ? { type: 'person', id: 'w-0015' } : { type: 'zone', id: next },
            );
      });
      expect(engine.store.getState().session).toBeNull();
      expect(overlays()).toEqual([
        expect.objectContaining({
          target: { type: 'zone', id: 'z-04' },
          box: expect.objectContaining({ min: [8, 1.2, 0] }),
        }),
      ]);
    },
  );

  it('F01: 세션 종료 전에 만든 지연 입력이 다른 구역을 변경하지 않는다', async () => {
    await mount();
    begin('z-04');
    const x = field();
    vi.useFakeTimers();
    fireEvent.focus(x);
    fireEvent.change(x, { target: { value: '99' } });
    select('z-02');
    begin('z-02');
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(engine.store.getState().session!.draft.min[0]).toBe(1);
  });

  it('F06: 자동 반영 후 Enter도 같은 의미로 보관하며 잘못된 값에서는 종료하지 않는다', async () => {
    await mount();
    begin('z-04');
    const x = field();
    vi.useFakeTimers();
    fireEvent.focus(x);
    fireEvent.change(x, { target: { value: '7' } });
    act(() => {
      vi.advanceTimersByTime(220);
    });
    fireEvent.keyDown(x, { key: 'Enter' });
    expect(engine.store.getState().session).toBeNull();
    begin('z-04');
    enter('-1', '폭 X (m)');
    expect(engine.store.getState().session).not.toBeNull();
    expect(screen.getByRole('alert').textContent).toContain('0보다 커야');
  });

  it('F08/F10: 같은 구역 재활성화는 현재 값을 유지하고 이번 수정 취소는 이전 보관값으로 돌아간다', async () => {
    await mount();
    begin('z-04');
    enter('7');
    begin('z-04');
    act(() => {
      engine.store.getState().updateField('originX', 8);
      viewer.getState().activate({ type: 'zone', id: 'z-04' });
    });
    expect(engine.store.getState().session!.draft.min[0]).toBe(8);
    fireEvent.click(screen.getByRole('button', { name: '이번 수정 취소' }));
    expect(overlays()[0]?.box.min[0]).toBe(7);
    expect(screen.getByRole('status').textContent).toContain('이전에 보관한 미리보기');
  });

  it('F11: 변경 없는 결과와 원본으로 되돌린 보관 초안은 미저장 항목으로 남지 않는다', async () => {
    await mount();
    begin('z-04');
    fireEvent.click(screen.getByRole('button', { name: '미리보기 유지' }));
    expect(overlays()).toHaveLength(0);
    begin('z-04');
    enter('7');
    begin('z-04');
    enter('5.8');
    expect(overlays()).toHaveLength(0);
  });

  it('F12/F13: Scene 버전을 되돌려도 폐기 초안은 재등장하지 않고 새 편집 안내를 가리지 않는다', async () => {
    await mount();
    begin('z-04');
    enter('7');
    const world = useRealtimeStore.getState().world!;
    act(() => {
      useRealtimeStore.setState({
        world: { ...world, payload: { ...world.payload, scene_version: 'scene-new' } },
      });
    });
    expect(overlays()).toHaveLength(0);
    act(() => {
      useRealtimeStore.setState({ world });
    });
    expect(overlays()).toHaveLength(0);
    begin('z-04');
    enter('8');
    expect(screen.getByRole('status').textContent).toContain('미리보기를 유지');
  });

  it('F12: 권한을 잃었다가 복귀해도 보관 초안은 살아나지 않는다', async () => {
    await mount();
    begin('z-04');
    enter('7');
    act(() => {
      useSessionStore.setState({
        session: { accessToken: 'test2', user: { id: 'admin', role: 'VIEWER' } },
      });
    });
    expect(overlays()).toHaveLength(0);
    act(() => {
      useSessionStore.setState({
        session: { accessToken: 'test3', user: { id: 'admin', role: 'ADMIN' } },
      });
    });
    expect(overlays()).toHaveLength(0);
  });

  it('F20: 다른 메뉴로 나가 화면이 내려가도 보관 초안과 편집 중이던 미리보기가 남고, 다시 들어오면 그대로 보인다', async () => {
    const first = await mount();
    begin('z-04');
    enter('7');
    begin('z-02');
    act(() => {
      engine.store.getState().updateField('originX', 2);
    });
    // /cctv 등 화면을 채우지 않는 메뉴로 가면 AppLayout 이 Host 를 내린다.
    first.unmount();
    expect(Object.keys(useZoneDraftsStore.getState().drafts).sort()).toEqual(['z-02', 'z-04']);

    overlay = null;
    await mount();
    const byId = Object.fromEntries(overlays().map((o) => [o.target.id, o.box.min[0]]));
    expect(byId).toEqual({ 'z-04': 7, 'z-02': 2 });
    const kept = useZoneDraftsStore.getState().drafts['z-02']!;
    expect(kept.before.min).toEqual([1, 6.2, 0]);
    expect(kept.provenance.sourceId).toBe('s-1');
    expect(screen.getByText(/전체 미저장\s*2\s*건/)).toBeTruthy();
  });

  it('F20: 화면이 내려가 있는 동안 Source 가 바뀌었으면 다시 들어와도 이전 초안을 보이지 않는다', async () => {
    const first = await mount();
    begin('z-04');
    enter('7');
    first.unmount();
    act(() => {
      useRealtimeStore.setState({ source: { id: 's-2', pushZones: vi.fn() } as never });
    });
    overlay = null;
    await mount();
    expect(overlays()).toHaveLength(0);
    expect(useZoneDraftsStore.getState().drafts).toEqual({});
  });

  it('F20: 화면이 떠 있을 때 세션 변경으로 보관 Store 를 비우면 화면의 초안도 사라진다', async () => {
    await mount();
    begin('z-04');
    enter('7');
    expect(overlays()).toHaveLength(1);
    act(() => {
      useZoneDraftsStore.getState().clear();
    });
    expect(overlays()).toHaveLength(0);
    expect(screen.queryByText(/전체 미저장/)).toBeNull();
  });

  it('F12: Source A/B/A가 한 번에 바뀌어도 폐기된 보관 초안이 복구되지 않는다', async () => {
    await mount();
    begin('z-04');
    enter('7');
    const original = useRealtimeStore.getState().source;
    act(() => {
      useRealtimeStore.setState({ source: { id: 's-2', pushZones: vi.fn() } as never });
      useRealtimeStore.setState({ source: original });
    });
    expect(overlays()).toHaveLength(0);
  });

  it.each(['삭제', '형상 변경'])(
    'F12: 원본 %s 후 원본이 돌아와도 보관 초안은 재등장하지 않는다',
    async (kind) => {
      const page = await mount();
      begin('z-04');
      enter('7');
      const original = input.zones;
      input.zones =
        kind === '삭제'
          ? original.filter((z) => z.zoneId !== 'z-04')
          : original.map((z) => (z.zoneId === 'z-04' ? { ...z, min: [5.9, 1.2, 0] } : z));
      input.raw = {
        contract: 'DEMO_UNCONFIRMED',
        zones: input.zones.map((z) => ({
          zone_id: z.zoneId,
          aabb: { min_xyz: z.min, max_xyz: z.max },
        })),
      };
      page.rerender(
        <SpatialViewportHost loadViewer={load} zoneEditor>
          <ZonesPage />
        </SpatialViewportHost>,
      );
      expect(overlays()).toHaveLength(0);
      input.zones = original;
      input.raw = {
        contract: 'DEMO_UNCONFIRMED',
        zones: original.map((z) => ({
          zone_id: z.zoneId,
          aabb: { min_xyz: z.min, max_xyz: z.max },
        })),
      };
      page.rerender(
        <SpatialViewportHost loadViewer={load} zoneEditor>
          <ZonesPage />
        </SpatialViewportHost>,
      );
      expect(overlays()).toHaveLength(0);
    },
  );

  it('F14/F15: 선택 중에도 전체 보관 목록에 접근하고 전체 복원은 확인 뒤에만 실행한다', async () => {
    await mount();
    begin('z-04');
    enter('7');
    begin('z-02');
    enter('2');
    expect(screen.getByText('전체 미저장 2건')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '전체 복원' }));
    fireEvent.click(screen.getByRole('button', { name: '돌아가기' }));
    expect(overlays()).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: '전체 복원' }));
    fireEvent.click(screen.getByRole('button', { name: '모두 원본 복원' }));
    expect(overlays()).toHaveLength(0);
  });

  it('F17: 검색창 Escape는 목록만 닫고 활성 편집을 유지한다', async () => {
    await mount();
    begin('z-04');
    fireEvent.click(screen.getByRole('button', { name: /구역 목록/ }));
    fireEvent.keyDown(screen.getByLabelText('구역 검색'), { key: 'Escape' });
    expect(screen.queryByRole('region', { name: '구역 목록 선택' })).toBeNull();
    expect(engine.store.getState().session).not.toBeNull();
  });
});
