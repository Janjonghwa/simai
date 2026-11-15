import { useCamSplitStore } from '@/features/cctv/camSplit.store';
import { AppLayout } from '@/features/layout/AppLayout';
import { useHealthSnapshot } from '@/features/layout/useHealthSnapshot';
import { usePpeSnapshot } from '@/features/ppe/usePpe';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 레이아웃이 알림 자리를 어디에 어떻게 두는지만 본다. 화면 조각은 대역으로 바꾼다.
const route = vi.hoisted(() => ({
  staticData: {} as { fullBleed?: boolean; eventPanel?: boolean; dockOverlay?: boolean },
  alarm: true,
}));

/** AppLayout 안의 사람 표시 이름 어댑터(-658)가 워치 배정 캐시(react-query)를 읽어 QueryClient 가 필요하다 */
function renderLayout() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AppLayout />
    </QueryClientProvider>,
  );
}

vi.mock('@tanstack/react-router', () => ({
  useMatches: ({ select }: { select: (matches: unknown[]) => unknown }) =>
    select([{ staticData: route.staticData }]),
  Outlet: () => <div data-testid="page" />,
}));
vi.mock('@/features/layout/TopBar', () => ({ TopBar: () => <header /> }));
vi.mock('@/features/layout/Dock', () => ({ Dock: () => null }));
vi.mock('@/features/layout/SpatialViewportHost', () => ({
  SpatialViewportHost: ({ children }: { children?: ReactNode }) => children,
}));
vi.mock('@/features/events/AlarmSoundLoop', () => ({ AlarmSoundLoop: () => null }));
vi.mock('@/features/monitor/WorkSessionBar', () => ({
  WorkSessionBar: () => <div data-testid="session" />,
}));
vi.mock('@/features/monitor/PpePolicyNotice', () => ({ PpePolicyNotice: () => null }));
vi.mock('@/features/monitor/WorkPpeStatus', () => ({ WorkPpeStatus: () => null }));
vi.mock('@/features/layout/useHealthSnapshot', () => ({ useHealthSnapshot: vi.fn() }));
vi.mock('@/features/ppe/usePpe', () => ({ usePpeSnapshot: vi.fn() }));
vi.mock('@/features/events/AlarmBanner', () => ({
  AlarmBanner: ({ withPanel }: { withPanel: boolean }) =>
    route.alarm ? <div data-testid="alarm" data-with-panel={String(withPanel)} /> : null,
}));

const notices = () => screen.getByRole('region', { name: '알림' });
const ids = (el: HTMLElement) => [...el.children].map((child) => child.getAttribute('data-testid'));

beforeEach(() => {
  route.staticData = {};
  route.alarm = true;
});
afterEach(() => {
  cleanup();
  useCamSplitStore.getState().close();
});

describe('AppLayout 알림 자리', () => {
  it('실시간 관제(화면을 채움)에서는 장면 위에 띄우고, 경보 배너 아래에 작업 세션 바를 두며 양쪽 370px 을 비워 가운데에 선다', () => {
    route.staticData = { fullBleed: true, eventPanel: true };
    renderLayout();

    const box = notices();
    expect(ids(box)).toEqual(['alarm', 'session']);
    expect(box.className).toContain('absolute');
    expect(box.className).toContain('inset-x-(--event-panel-inset)');
    expect(box.className).not.toContain('inset-x-5');
    // 띄운 자리는 클릭을 통과시키고, 배너만 받는다(각자 pointer-events-auto).
    expect(box.className).toContain('pointer-events-none');
    expect(screen.getByTestId('alarm').dataset.withPanel).toBe('true');
  });

  it('사건 분할 창이 열리면 양쪽을 그 창 자리만큼 같은 크기로 비워 가운데를 지키고, 닫으면 되돌린다', () => {
    route.staticData = { fullBleed: true, eventPanel: true };
    renderLayout();
    expect(notices().className).toContain('inset-x-(--event-panel-inset)');

    act(() => useCamSplitStore.getState().open({ cam: 'cam_A', target: null }));
    // 왼쪽·오른쪽을 따로 정하지 않고 inset-x 하나로 같은 값을 준다.
    expect(notices().className).toContain('inset-x-(--cam-split-inset)');
    expect(notices().className).not.toContain('inset-x-(--event-panel-inset)');

    act(() => useCamSplitStore.getState().close());
    expect(notices().className).toContain('inset-x-(--event-panel-inset)');
  });

  it('이벤트 패널이 없는 화면은 사건 분할 창이 열려 있어도 양옆을 비키지 않는다', () => {
    route.staticData = {};
    renderLayout();
    act(() => useCamSplitStore.getState().open({ cam: 'cam_A', target: null }));
    expect(notices().className).toContain('inset-x-5');
  });

  it('다른 화면에서도 본문 위에 띄워 본문 높이를 차지하지 않는다(이벤트 패널이 없어 양옆을 비키지 않는다)', () => {
    route.staticData = {};
    renderLayout();

    const box = notices();
    expect(ids(box)).toEqual(['alarm']);
    expect(box.className).toContain('absolute');
    expect(box.className).toContain('pointer-events-none');
    expect(box.className).toContain('inset-x-5');
    expect(box.className).not.toContain('inset-x-(--event-panel-inset)');
    // 본문(main) 흐름 밖, 본문 뒤에 둔다.
    const page = screen.getByRole('main');
    expect(page.contains(box)).toBe(false);
    expect(page.compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('알릴 것이 없으면 자리가 비어 숨는다(empty:hidden)', () => {
    route.alarm = false;
    renderLayout();

    const box = notices();
    expect(box.childElementCount).toBe(0);
    expect(box.className).toContain('empty:hidden');
  });
});

describe('AppLayout 본문 아래 여백', () => {
  it('보통 화면은 하단 메뉴(독) 자리를 비우고, dockOverlay 화면은 독이 본문 위에 떠서 아래 여백을 줄인다', () => {
    renderLayout();
    expect(screen.getByRole('main').className).toContain('pb-[90px]');
    cleanup();

    route.staticData = { dockOverlay: true };
    renderLayout();
    expect(screen.getByRole('main').className).toContain('pb-3.5');
    expect(screen.getByRole('main').className).not.toContain('pb-[90px]');
  });
});

describe('AppLayout 연결 상태', () => {
  it('연결이 열릴 때 지금 상태를 먼저 받는 훅(GET /api/health)을 부른다', () => {
    renderLayout();
    expect(vi.mocked(useHealthSnapshot)).toHaveBeenCalled();
  });

  it('연결이 열릴 때 지금 PPE 상태를 먼저 받는 훅(GET /api/ppe/current)을 부른다(-531)', () => {
    renderLayout();
    expect(vi.mocked(usePpeSnapshot)).toHaveBeenCalled();
  });
});
