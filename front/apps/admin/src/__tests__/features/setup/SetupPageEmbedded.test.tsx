import { useCctvStore } from '@/features/cctv/cctv.store';
import { useSpatialViewport } from '@/features/layout/SpatialViewportHost';
import { markSetupZonesCommitted, resetSetupFlowStore } from '@/features/setup/setup.store';
import { SetupPage } from '@/features/setup/SetupPage';
import type { SetupSessionResponse } from '@/features/setup/types';
import type { Viewer3DHandle, Viewer3DProps, ViewerStatus } from '@/features/viewer3d';
import { useWorkActionState, WorkActionProvider } from '@/features/work/WorkActionContext';
import { sceneHandlers } from '@/mocks/scenes';
import { buildCoverageGuidePayload, createSetupMock } from '@/mocks/setup';
import { createStaticObjectsMock } from '@/mocks/staticObjects';
import { createZonesMock } from '@/mocks/zones';
import { useZoneEditStore } from '@/store/zoneEdit.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { delay, http } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// 세팅 화면을 날짜 화면 안에 끼운 모양(embedded): 자기 제목·머리줄 단추 없이 단추를 아래 오른쪽 단추 줄(useWorkAction)에 등록한다.
// 단계별 라벨·켜짐은 단독 화면(SetupPage.test.tsx)의 머리줄 단추와 같아야 한다. 3D 뷰어·구역 편집 도구는 대역으로 둔다.

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return {
    api: createHttp({ baseUrl: 'http://api.invalid', getToken: () => 'mock-token.admin' }),
  };
});
const navigateMock = vi.hoisted(() => vi.fn());
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => navigateMock }));
vi.mock('@/features/viewer3d', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/viewer3d')>();
  const { useEffect, useImperativeHandle, useRef } = await import('react');
  function FakeViewer({ ref, onStatusChange }: Viewer3DProps) {
    useImperativeHandle(
      ref,
      () =>
        ({
          select: () => ({ ok: true }),
          focus: () => ({ ok: true }),
          clearSelection: () => ({ ok: true }),
        }) as unknown as Viewer3DHandle,
    );
    const reportedRef = useRef(false);
    useEffect(() => {
      if (reportedRef.current) return;
      reportedRef.current = true;
      onStatusChange?.({ phase: 'ready', mode: 'LIVE', error: null } as ViewerStatus);
    }, [onStatusChange]);
    return <div data-testid="viewer3d" />;
  }
  return { ...actual, Viewer3D: FakeViewer };
});
vi.mock('@/features/zones/ZonesPage', () => ({
  ZonesPage: ({ setupLayoutSync }: { setupLayoutSync?: { sessionId: string } }) => {
    useSpatialViewport();
    return (
      <div data-testid="zones-page-stub" data-setup-session={setupLayoutSync?.sessionId ?? ''} />
    );
  },
}));
vi.mock('@/features/cctv/useCctvPreview', () => ({
  useCctvPreview: () => ({ attach: vi.fn(), retry: vi.fn() }),
}));

let setupMock = createSetupMock();
const baseHandlers = () => {
  const objects = createStaticObjectsMock();
  const zonesMock = createZonesMock({ staticObjects: objects.store });
  return [...zonesMock.handlers, ...sceneHandlers, ...objects.handlers];
};

const server = setupServer(...baseHandlers(), ...setupMock.handlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
beforeEach(() => {
  useCctvStore.getState().reset();
  resetSetupFlowStore();
  useZoneEditStore.getState().clear();
  navigateMock.mockReset();
  setupMock = createSetupMock({
    cameraHeightsM: { cam1: 2.25, cam2: 2.3, cam3: 2.2, cam4: null },
    latestGuide: buildCoverageGuidePayload({ connectivity: 'DISCONNECTED' }),
  });
  server.resetHandlers(...baseHandlers(), ...setupMock.handlers);
});
afterEach(() => {
  cleanup();
  server.events.removeAllListeners();
});
afterAll(() => server.close());

const POLL_WAIT = { timeout: 8_000 };

/** 날짜 화면 아래 오른쪽 단추 줄 대역(WorkActionLayer 자리): 등록된 단추를 그대로 그린다. */
function ActionLine() {
  const config = useWorkActionState();
  if (!config) return null;
  return (
    <div data-testid="action-line">
      {config.secondary?.map((item) => (
        <button key={item.label} type="button" disabled={item.disabled} onClick={item.onClick}>
          {item.label}
        </button>
      ))}
      <button
        type="button"
        disabled={!config.enabled}
        title={config.title}
        onClick={config.onClick}
      >
        {config.label}
      </button>
    </div>
  );
}

function renderEmbedded(onDone = vi.fn(), workUnitId = 1) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <WorkActionProvider>
        <div data-testid="page">
          <SetupPage embedded workUnitId={workUnitId} onDone={onDone} />
        </div>
        <ActionLine />
      </WorkActionProvider>
    </QueryClientProvider>,
  );
  return { ...view, onDone };
}

/** 단추 줄에서 이름이 같은 단추. */
const line = () => within(screen.getByTestId('action-line'));
/** 단추 줄의 단추 이름을 왼쪽부터(주 단추가 맨 뒤). */
const lineLabels = () =>
  within(screen.getByTestId('action-line'))
    .getAllByRole('button')
    .map((button) => button.textContent);
const mainButton = () => {
  const buttons = within(screen.getByTestId('action-line')).getAllByRole('button');
  return buttons[buttons.length - 1] as HTMLButtonElement;
};

/** 1단계에서 cam4 높이를 채워 '다음' → 방식 고르기 '새로 3D 공간 만들기' 로 세션을 만들고 그 세션 id 를 돌려준다. */
async function startSetupSession() {
  const view = renderEmbedded();
  const cam4 = (await screen.findByLabelText('카메라 4 높이 (m)')) as HTMLInputElement;
  await waitFor(() => expect(mainButton().disabled).toBe(true));
  fireEvent.change(cam4, { target: { value: '2.35' } });
  await waitFor(() => expect(mainButton().disabled).toBe(false));
  fireEvent.click(mainButton());
  const dialog = screen.getByRole('dialog', { name: '3D 공간' });
  fireEvent.click(within(dialog).getByRole('button', { name: /^새로 3D 공간 만들기/ }));
  await waitFor(() => expect(setupMock.sessions.size).toBe(1));
  const sessionId = [...setupMock.sessions.keys()][0]!;
  expect(await screen.findByText(`세션 ${sessionId}`)).toBeTruthy();
  return { ...view, sessionId };
}

const PREVIEW_PATCH: Partial<SetupSessionResponse> = {
  stage: 'PREVIEW_AND_VLM_DRAFT',
  status: 'PREVIEW_AND_VLM_DRAFT',
  progress_pct: 92,
  preview_scene_version: 'scene-preview-1',
  calibration_version: 'cal-8',
};

/** 맵이 준비돼 켜진 주 단추(다음)로 구역 단계에 간다. */
async function goToZoneStep() {
  await waitFor(() => expect(mainButton().disabled).toBe(false), POLL_WAIT);
  fireEvent.click(mainButton());
  return screen.findByTestId('zones-page-stub', undefined, POLL_WAIT);
}

describe('세팅 화면 임베드', () => {
  it('자기 제목과 머리줄 단추를 그리지 않고 1단계 다음을 아래 단추 줄에 등록한다(세션 줄·단계 줄은 남는다)', async () => {
    renderEmbedded();
    await screen.findByLabelText('카메라 4 높이 (m)');

    const page = within(screen.getByTestId('page'));
    expect(page.queryByRole('heading', { level: 1 })).toBeNull();
    expect(page.queryByRole('button', { name: '다음' })).toBeNull();
    expect(page.getByText('세팅 1단계 · 카메라 확인')).toBeTruthy();
    const steps = within(page.getByRole('list', { name: '보정 단계' }));
    expect(steps.getAllByRole('listitem').map((step) => step.textContent)).toEqual([
      '1카메라 연결 관리',
      '2보정·생성',
      '3구역',
    ]);

    expect(lineLabels()).toEqual(['다음']);
    expect(mainButton().disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('카메라 4 높이 (m)'), { target: { value: '2.35' } });
    await waitFor(() => expect(mainButton().disabled).toBe(false));
  });

  it('보정·생성에서는 세팅 취소가 보조 단추이고, 다음은 맵이 준비되면 켜진다', async () => {
    const { sessionId } = await startSetupSession();

    expect(lineLabels()).toEqual(['세팅 취소', '다음']);
    expect(mainButton().disabled).toBe(true);
    expect(
      within(screen.getByTestId('page')).queryByRole('button', { name: '세팅 취소' }),
    ).toBeNull();

    setupMock.updateSession(sessionId, PREVIEW_PATCH);
    await waitFor(() => expect(mainButton().disabled).toBe(false), POLL_WAIT);
    expect(lineLabels()).toEqual(['세팅 취소', '다음']);
  }, 20_000);

  it('보조 단추 세팅 취소는 확인한 뒤 세션을 취소한다', async () => {
    const { sessionId } = await startSetupSession();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      fireEvent.click(line().getByRole('button', { name: '세팅 취소' }));
      await waitFor(() => expect(setupMock.sessions.get(sessionId)?.status).toBe('FAILED'));
      expect(confirm).toHaveBeenCalledTimes(1);
    } finally {
      confirm.mockRestore();
    }
  }, 20_000);

  it('세션 취소를 요청하는 동안 세팅 취소는 꺼진다(단독 화면과 같다)', async () => {
    const { sessionId } = await startSetupSession();
    server.use(http.post('*/api/setup-sessions/:id/cancel', () => delay('infinite')));
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      const cancel = () => line().getByRole('button', { name: '세팅 취소' }) as HTMLButtonElement;
      expect(cancel().disabled).toBe(false);
      fireEvent.click(cancel());
      await waitFor(() => expect(cancel().disabled).toBe(true));
      expect(setupMock.sessions.get(sessionId)?.status).not.toBe('FAILED');
    } finally {
      confirm.mockRestore();
    }
  }, 20_000);

  it('높이 불일치 안내가 뜨면 카메라 다시 맞추기가 보조 단추로 생기고 누르면 1단계로 돌아간다', async () => {
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, {
      ...PREVIEW_PATCH,
      height_checks: {
        cam2: {
          input_height_m: 2.3,
          restored_height_m: 2.71,
          difference_cm: 41,
          within_tolerance: false,
          guidance: 'cam2 높이를 다시 재서 입력해 주세요',
        },
      },
    });
    await screen.findByRole('alert', { name: '카메라 높이 불일치 경고' }, POLL_WAIT);
    expect(lineLabels()).toEqual(['세팅 취소', '카메라 다시 맞추기', '다음']);

    fireEvent.click(line().getByRole('button', { name: '카메라 다시 맞추기' }));
    expect(await screen.findByLabelText('카메라 2 높이 (m)')).toBeTruthy();
    expect(lineLabels()).toEqual(['다음']);
  }, 20_000);

  it('구역 단계: 다시 보정이 보조 단추이고, 구역을 발행하기 전 다음은 꺼져 있다가 발행하면 켜진다', async () => {
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, PREVIEW_PATCH);
    await goToZoneStep();

    expect(lineLabels()).toEqual(['다시 보정', '다음']);
    expect(mainButton().disabled).toBe(true);
    expect(mainButton().title).not.toBe('');
    expect(screen.queryByRole('button', { name: '작업 관리로' })).toBeNull();

    act(() => markSetupZonesCommitted());
    expect(lineLabels()).toEqual(['다시 보정', '다음']);
    expect(mainButton().disabled).toBe(false);
    expect(mainButton().title).toBe('');
  }, 20_000);

  it('구역 단계의 다음은 onDone 을 부르고 작업 관리로 이동하지 않는다', async () => {
    const { sessionId, onDone } = await startSetupSession();
    setupMock.updateSession(sessionId, PREVIEW_PATCH);
    await goToZoneStep();
    act(() => markSetupZonesCommitted());

    fireEvent.click(mainButton());
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(navigateMock).not.toHaveBeenCalled();
  }, 20_000);

  it('구역을 이미 발행한 세션을 이어받으면 구역 단계에 들어서자 다음이 켜져 있다', async () => {
    const { sessionId, onDone } = await startSetupSession();
    setupMock.updateSession(sessionId, { ...PREVIEW_PATCH, zones_committed_at: 1_793_848_135_600 });
    await goToZoneStep();

    expect(mainButton().disabled).toBe(false);
    fireEvent.click(mainButton());
    expect(onDone).toHaveBeenCalledTimes(1);
  }, 20_000);

  it('구역 단계의 다시 보정은 1단계로 돌아간다', async () => {
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, PREVIEW_PATCH);
    await goToZoneStep();

    fireEvent.click(line().getByRole('button', { name: '다시 보정' }));
    expect(await screen.findByLabelText('카메라 4 높이 (m)')).toBeTruthy();
    expect(lineLabels()).toEqual(['다음']);
  }, 20_000);

  it('날짜 작업에는 레이아웃이 없어서 구역 저장에 세션 id 는 싣되 가상 레이아웃 반영은 요청하지 않고 실패 안내도 띄우지 않는다', async () => {
    const layoutRequests: string[] = [];
    server.events.on('request:start', ({ request }) => {
      if (request.url.includes('/layout/zones')) layoutRequests.push(request.url);
    });
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, PREVIEW_PATCH);
    await goToZoneStep();

    // 구역 저장이 세션에 이어지도록 세션 id 는 그대로 넘긴다(setup_session_id).
    expect(screen.getByTestId('zones-page-stub').getAttribute('data-setup-session')).toBe(
      sessionId,
    );
    expect(layoutRequests).toEqual([]);
    expect(screen.queryByText(/가상 레이아웃 반영에 실패/)).toBeNull();
  }, 20_000);
});

describe('단독 화면은 단추 줄을 쓰지 않는다', () => {
  it('제목과 머리줄 단추를 그대로 그리고 단추 줄에는 아무것도 등록하지 않는다', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <WorkActionProvider>
          <div data-testid="page">
            <SetupPage workUnitId={1} />
          </div>
          <ActionLine />
        </WorkActionProvider>
      </QueryClientProvider>,
    );
    await screen.findByLabelText('카메라 4 높이 (m)');
    const page = within(screen.getByTestId('page'));
    expect(page.getByRole('heading', { level: 1, name: '설치 · 보정' })).toBeTruthy();
    expect(page.getByRole('button', { name: '다음' })).toBeTruthy();
    expect(screen.queryByTestId('action-line')).toBeNull();
  });
});
