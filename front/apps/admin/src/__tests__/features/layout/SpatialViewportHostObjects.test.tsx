import {
  SpatialViewportHost,
  targetOfKey,
  useSpatialViewport,
} from '@/features/layout/SpatialViewportHost';
import type { Viewer3DHandle, Viewer3DProps, ViewerStatus } from '@/features/viewer3d';
import { useObjectEditStore } from '@/store/objectEdit.store';
import { useRealtimeStore } from '@/store/realtime.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen } from '@testing-library/react';
import { useImperativeHandle, useLayoutEffect, useRef, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

// 틀이 작업면 선택 키·편집 알림을 작업면 등록 화면(-165)에 잇는지 본다. 작업면을 받고 3D 에 넣는 일은 그 화면이
// 맡으므로 틀은 하지 않는다(관제·구역 화면은 그대로). 목록·구역은 관심 밖이다.
const ZONES_RAW = { contract: 'DEMO_UNCONFIRMED', zones: [] };

vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: [], loaded: true, failed: false }),
}));
vi.mock('@/features/zones/useZones', () => ({
  useZones: () => ({ zones: [], raw: ZONES_RAW, failed: false }),
}));
const api = vi.hoisted(() =>
  vi.fn<(path: string) => Promise<never>>(() => Promise.reject(new Error('no api in test'))),
);
const avatarMeshFetch = vi.hoisted(() => vi.fn<(url: string) => Promise<ArrayBuffer>>());
vi.mock('@/lib/api', () => ({ api }));
vi.mock('@/lib/avatarMeshFetch', () => ({ avatarMeshFetch }));

// 3D 뷰어 대역: 마운트마다 준비를 한 번 알리고, 마지막으로 받은 props 를 남긴다.
const fake = vi.hoisted(() => ({
  select: vi.fn(),
  props: null as Viewer3DProps | null,
}));

function FakeViewer(props: Viewer3DProps) {
  const { ref, onStatusChange } = props;
  useImperativeHandle(
    ref,
    () =>
      ({
        select: fake.select,
        focus: () => ({ ok: true }),
        clearSelection: () => ({ ok: true }),
        setMode: () => ({ ok: true }),
      }) as unknown as Viewer3DHandle,
  );
  const reported = useRef(false);
  useLayoutEffect(() => {
    fake.props = props;
    if (reported.current) return;
    reported.current = true;
    onStatusChange?.({ phase: 'ready', mode: 'LIVE', error: null } as ViewerStatus);
  });
  return <div data-testid="viewer3d" />;
}

const loadFake = () => Promise.resolve({ default: FakeViewer });

/** 틀이 화면에 주는 선택 키와 선택 명령을 시험에서 쓴다. */
function Probe() {
  const viewport = useSpatialViewport();
  return (
    <>
      <span data-testid="selected">{viewport?.selectedKey ?? ''}</span>
      <button onClick={() => viewport?.selectTarget({ type: 'object', id: 'ws-2' })}>
        작업면 고르기
      </button>
    </>
  );
}

function renderHost() {
  const queryClient = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(
    <SpatialViewportHost loadViewer={loadFake}>
      <Probe />
    </SpatialViewportHost>,
    { wrapper },
  );
}

let pushWorkSurfaces: Mock;

beforeEach(() => {
  pushWorkSurfaces = vi.fn();
  useRealtimeStore.setState(
    {
      ...useRealtimeStore.getInitialState(),
      source: { id: 's-1', pushZones: vi.fn(), pushWorkSurfaces } as never,
    },
    true,
  );
  useObjectEditStore.getState().clear();
  api.mockClear();
  fake.props = null;
  fake.select.mockReset().mockImplementation(() => ({ ok: true }));
});
afterEach(cleanup);

describe('틀의 작업면 연결', () => {
  it('3D 에 앱의 인증된 아바타 fetch 를 넘긴다', async () => {
    renderHost();
    await screen.findByTestId('viewer3d');
    expect(fake.props?.avatarMeshFetch).toBe(avatarMeshFetch);
  });

  it('틀은 작업면 목록을 받지 않고 3D 에 넣지도 않는다', async () => {
    renderHost();
    await screen.findByTestId('viewer3d');
    expect(api.mock.calls.some(([path]) => String(path).includes('/api/static-objects'))).toBe(
      false,
    );
    expect(pushWorkSurfaces).not.toHaveBeenCalled();
  });

  it('3D 작업면 편집의 미리보기·결과는 편집 저장소로 보낸다', async () => {
    renderHost();
    await screen.findByTestId('viewer3d');
    expect(fake.props?.onObjectEditPreview).toBe(useObjectEditStore.getState().onPreview);
    expect(fake.props?.onObjectEditResult).toBe(useObjectEditStore.getState().onResult);
  });

  it('3D 에서 고른 작업면은 object 키가 되고, 화면은 작업면을 대상으로 고를 수 있다', async () => {
    renderHost();
    await screen.findByTestId('viewer3d');
    act(() => fake.props?.onSelectionChange?.({ type: 'object', id: 'ws-1' }));
    expect(screen.getByTestId('selected').textContent).toBe('object:ws-1');

    act(() => screen.getByRole('button', { name: '작업면 고르기' }).click());
    expect(fake.select).toHaveBeenLastCalledWith({ type: 'object', id: 'ws-2' });
    expect(screen.getByTestId('selected').textContent).toBe('object:ws-2');
    expect(targetOfKey('object:ws-2')).toEqual({ type: 'object', id: 'ws-2' });
  });
});
