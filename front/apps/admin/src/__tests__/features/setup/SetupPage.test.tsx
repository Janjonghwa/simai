import { useCctvStore } from '@/features/cctv/cctv.store';
import { useSpatialViewport } from '@/features/layout/SpatialViewportHost';
import { requestCameraHeightDefaults } from '@/features/setup/api';
import {
  markSetupZonesCommitted,
  resetSetupFlowStore,
  useSetupFlowStore,
} from '@/features/setup/setup.store';
import { SetupPage } from '@/features/setup/SetupPage';
import type {
  Viewer3DHandle,
  Viewer3DProps,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d';
import { api } from '@/lib/api';
import { sceneHandlers } from '@/mocks/scenes';
import { buildCoverageGuidePayload, createSetupMock } from '@/mocks/setup';
import { createStaticObjectsMock } from '@/mocks/staticObjects';
import { createZonesMock } from '@/mocks/zones';
import { useZoneEditStore } from '@/store/zoneEdit.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return {
    // 세팅(초안 수락으로 구역 저장 포함)은 관리자가 한다.
    api: createHttp({ baseUrl: 'http://api.invalid', getToken: () => 'mock-token.admin' }),
  };
});
const navigateMock = vi.hoisted(() => vi.fn());
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => navigateMock }));
// 3D 뷰어 대역: 준비를 한 번 알리고 마지막 props 를 남긴다. 고르기·초점 명령을 기록하고, 고르면 3D 클릭처럼 선택을 알린다.
const fakeViewer = vi.hoisted(() => ({
  props: null as Viewer3DProps | null,
  select: vi.fn(),
  focus: vi.fn(),
}));
vi.mock('@/features/viewer3d', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/viewer3d')>();
  const { useEffect, useImperativeHandle, useRef } = await import('react');
  function FakeViewer(props: Viewer3DProps) {
    const { ref, onStatusChange, onSelectionChange } = props;
    fakeViewer.props = props;
    useImperativeHandle(
      ref,
      () =>
        ({
          select: (target: ViewerTarget) => {
            fakeViewer.select(target);
            onSelectionChange?.(target);
            return { ok: true };
          },
          focus: (target: ViewerTarget) => {
            fakeViewer.focus(target);
            return { ok: true };
          },
          clearSelection: () => ({ ok: true }),
        }) as unknown as Viewer3DHandle,
    );
    const reported = useRef(false);
    useEffect(() => {
      if (reported.current) return;
      reported.current = true;
      onStatusChange?.({ phase: 'ready', mode: 'LIVE', error: null } as ViewerStatus);
    }, [onStatusChange]);
    return <div data-testid="viewer3d" />;
  }
  return { ...actual, Viewer3D: FakeViewer };
});
// 구역 편집 도구 대역: 세팅의 구역 단계가 준 3D 틀 값(SpatialViewportContext)을 읽어 보인다.
vi.mock('@/features/zones/ZonesPage', () => ({
  ZonesPage: ({ placement }: { placement?: string }) => {
    const viewport = useSpatialViewport();
    return (
      <div
        data-testid="zones-page-stub"
        data-placement={placement}
        data-viewer-ready={String(viewport?.viewerReady)}
        data-zones={viewport?.zones.length ?? 0}
        data-selected={viewport?.selectedKey ?? ''}
      >
        <button
          onClick={() => viewport?.selectTarget({ type: 'zone', id: 'z-04' }, { focus: true })}
        >
          z-04 고르기
        </button>
      </div>
    );
  },
}));
vi.mock('@/features/cctv/useCctvPreview', () => ({
  useCctvPreview: () => ({ attach: vi.fn(), retry: vi.fn() }),
}));

let setupMock = createSetupMock();
// 구역·정적 객체 mock 은 초안 수락이 구역을 저장하므로 시험마다 새로 만든다. 구역 단계 3D 는 현재 배경·정적 객체를 받는다.
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
  fakeViewer.props = null;
  fakeViewer.select.mockReset();
  fakeViewer.focus.mockReset();
  setupMock = createSetupMock({
    cameraHeightsM: {
      cam1: 2.25,
      cam2: 2.3,
      cam3: 2.2,
      cam4: null,
    },
    latestGuide: buildCoverageGuidePayload({
      connectivity: 'DISCONNECTED',
      guides: [
        {
          camera_id: 'cam3',
          pair: ['cam3', 'cam4'],
          direction: 'RIGHT',
          magnitude: 'SMALL',
          reason_code: 'WEAK_PAIR_EDGE_CLUSTER',
        },
      ],
    }),
  });
  server.resetHandlers(...baseHandlers(), ...setupMock.handlers);
});
afterEach(() => {
  cleanup();
  server.events.removeAllListeners();
});
afterAll(() => server.close());

function renderSetupPage(workUnitId: number | null = 1) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <SetupPage workUnitId={workUnitId ?? undefined} />
      </QueryClientProvider>,
    ),
  };
}

describe('설치 · 보정 작업 연결', () => {
  it('작업 id 없이 열면 세팅을 시작하지 않고 작업 관리로 돌려보내는 안내를 보인다', () => {
    renderSetupPage(null);
    expect(screen.getByTestId('setup-needs-work')).toBeTruthy();
    expect(screen.queryByLabelText('cam1 높이 (m)')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '작업 관리로 이동' }));
    expect(navigateMock).toHaveBeenCalledWith({ to: '/work' });
    expect(setupMock.submittedBodies).toHaveLength(0);
  });

  it('검색 값의 workUnitId 로 세팅을 시작하고 발행 뒤에는 그 작업의 작업 관리로 돌아간다', async () => {
    renderSetupPage(5);
    const cam4 = (await screen.findByLabelText('cam4 높이 (m)')) as HTMLInputElement;
    fireEvent.change(cam4, { target: { value: '2.35' } });
    fireEvent.click(screen.getByRole('button', { name: '그대로 진행' }));
    await waitFor(() => expect(setupMock.submittedBodies).toHaveLength(1));
    expect(setupMock.submittedBodies[0]).toMatchObject({ work_unit_id: 5 });
    expect(useSetupFlowStore.getState().workUnitId).toBe(5);
  });
});

describe('세팅 화면 재진입', () => {
  it('작업에 진행 중 세션이 있으면 새로 시작하지 않고 이어받아 그 세션 대기 화면을 연다', async () => {
    const resumeId = '019a0000-0000-7000-8000-0000000000aa';
    const resumeMock = createSetupMock({ workUnitSessionId: resumeId });
    server.resetHandlers(...baseHandlers(), ...resumeMock.handlers);
    renderSetupPage(5);
    await waitFor(() => expect(useSetupFlowStore.getState().activeSessionId).toBe(resumeId));
    expect(resumeMock.submittedBodies).toHaveLength(0);
    expect(screen.queryByText(/cal-8/)).toBeNull();
  });
});

describe('설치 · 보정 1단계 화면', () => {
  it('본문은 남은 높이를 채우는 한 덩어리로 묶고 자기 아래 여백은 두지 않는다(주 메뉴 여백은 틀이 둔다)', () => {
    const { container } = renderSetupPage();
    const page = container.firstElementChild!;
    expect(page.classList.contains('flex-1')).toBe(true);
    expect(page.classList.contains('min-h-0')).toBe(true);
    expect(page.className).not.toContain('pb-[');
    expect(page.contains(screen.getByLabelText('cam1 높이 (m)'))).toBe(true);
  });

  it('보정 단계 아래에 지도와 카메라 영상을 한 카드로 보이고 규칙 표는 두지 않는다(관리자가 아닌 역할도)', async () => {
    renderSetupPage();
    expect(await screen.findByRole('heading', { name: '카메라 시야 · 실시간 영상' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: '실시간 CCTV 4칸' })).toBeNull();
    expect(screen.queryByRole('heading', { name: '카메라 쌍 시야 연결' })).toBeNull();
    expect(screen.getByRole('list', { name: '보정 단계' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: '규칙' })).toBeNull();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('세션이 생기기 전에는 2·3단계로 갈 수 없어 카메라 연결 안내가 한 번만 보인다', async () => {
    renderSetupPage();
    await screen.findByLabelText('cam1 높이 (m)');
    const steps = within(screen.getByRole('list', { name: '보정 단계' })).getAllByRole('button');
    expect((steps[0] as HTMLButtonElement).disabled).toBe(false);
    expect((steps[1] as HTMLButtonElement).disabled).toBe(true);
    expect((steps[2] as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(steps[1]!);
    expect(screen.getAllByRole('heading', { name: '카메라 연결 · 방향 안내' })).toHaveLength(1);
    expect(screen.getByLabelText('cam1 높이 (m)')).toBeTruthy();
  });

  it('카메라 높이 기본값을 자동으로 채우고 값 없는 카메라만 입력 필수 상태로 둔다', async () => {
    renderSetupPage();

    const cam1Input = (await screen.findByLabelText('cam1 높이 (m)')) as HTMLInputElement;
    await waitFor(() => expect(cam1Input.value).toBe('2.25'));

    const cam2Input = screen.getByLabelText('cam2 높이 (m)') as HTMLInputElement;
    const cam3Input = screen.getByLabelText('cam3 높이 (m)') as HTMLInputElement;
    const cam4Input = screen.getByLabelText('cam4 높이 (m)') as HTMLInputElement;

    expect(cam2Input.value).toBe('2.3');
    expect(cam3Input.value).toBe('2.2');
    expect(cam4Input.value).toBe('');
    expect(cam1Input.required).toBe(false);
    expect(cam4Input.required).toBe(true);

    expect(
      screen.getByText(
        '거치대 기둥을 늘 같은 길이로 펴고(테이프 표시 권장), 바꿨을 때만 다시 재세요',
      ),
    ).toBeTruthy();
    expect(screen.getByText('입력 필수')).toBeTruthy();
    expect(screen.getAllByText('지난 제출값')).toHaveLength(3);

    const submitButton = screen.getByRole('button', { name: '그대로 진행' }) as HTMLButtonElement;
    expect(submitButton.disabled).toBe(true);
  });

  it('값 없는 카메라 높이를 채워 제출하면 요청 본문을 보내고 다음 기본값으로 반영한다', async () => {
    renderSetupPage();

    const cam1Input = (await screen.findByLabelText('cam1 높이 (m)')) as HTMLInputElement;
    await waitFor(() => expect(cam1Input.value).toBe('2.25'));

    const cam4Input = screen.getByLabelText('cam4 높이 (m)') as HTMLInputElement;
    fireEvent.change(cam4Input, { target: { value: '2.35' } });

    const submitButton = screen.getByRole('button', { name: '그대로 진행' }) as HTMLButtonElement;
    expect(submitButton.disabled).toBe(false);

    fireEvent.click(submitButton);

    await waitFor(() => expect(setupMock.submittedBodies).toHaveLength(1));
    expect(setupMock.submittedBodies[0]).toEqual({
      work_unit_id: 1,
      camera_heights_m: {
        cam1: 2.25,
        cam2: 2.3,
        cam3: 2.2,
        cam4: 2.35,
      },
    });

    // 제출한 높이가 다음 기본값 조회에도 반영되고 2단계로 넘어간다.
    expect(await screen.findByText(/세션 019a0000/)).toBeTruthy();
    const nextDefaults = await requestCameraHeightDefaults(api);
    expect(nextDefaults.all_present).toBe(true);
    expect(nextDefaults.camera_heights_m).toEqual({
      cam1: 2.25,
      cam2: 2.3,
      cam3: 2.2,
      cam4: 2.35,
    });
  });

  it('연결 상태 3종(DISCONNECTED / CONNECTED / UNKNOWN), 쌍 등급, 방향 안내 문구를 표시한다', async () => {
    renderSetupPage();

    expect(await screen.findByText('DISCONNECTED · 시야 분리됨')).toBeTruthy();
    expect(
      screen.getAllByText('cam3 카메라를 오른쪽으로 조금 돌려 주세요 (cam3–cam4)').length,
    ).toBeGreaterThan(0);
    // 쌍 등급은 목록 없이 지도의 연결선에만 보인다.
    const edges = within(screen.getByLabelText('카메라 쌍 연결 선'));
    expect(edges.getAllByLabelText(/충분$/).length).toBeGreaterThan(0);
    expect(edges.getAllByLabelText(/약함$/).length).toBeGreaterThan(0);
    expect(edges.getAllByLabelText(/부족$/).length).toBeGreaterThan(0);
    expect(screen.queryByRole('list', { name: '카메라 쌍 매칭 등급' })).toBeNull();

    // CONNECTED 상태로 바꾸면 "CONNECTED · 4대 시야 연결됨" 과 "다음" 버튼을 보인다
    cleanup();
    setupMock.state.latestGuide = buildCoverageGuidePayload({
      connectivity: 'CONNECTED',
      guides: [],
    });
    renderSetupPage();

    expect(await screen.findByText('CONNECTED · 4대 시야 연결됨')).toBeTruthy();
    expect(screen.getByRole('button', { name: '다음' })).toBeTruthy();
  });

  it('STALE·영상 없음(NO_VIDEO)·계산 실패(UNKNOWN) 상태를 표시하고 그대로 진행을 막지 않는다', async () => {
    setupMock = createSetupMock({
      cameraHeightsM: { cam1: 2.2, cam2: 2.2, cam3: 2.2, cam4: 2.2 },
      guideStatus: 'STALE',
      guideStale: true,
      latestGuide: buildCoverageGuidePayload({
        connectivity: 'UNKNOWN',
        cameraStatuses: { cam2: 'NO_VIDEO' },
        guides: [
          {
            camera_id: 'cam2',
            pair: ['cam2', 'cam3'],
            direction: 'NONE',
            magnitude: 'NONE',
            reason_code: 'NO_OVERLAP_VISIBLE',
          },
        ],
      }),
    });
    server.resetHandlers(...baseHandlers(), ...setupMock.handlers);

    renderSetupPage();

    expect(await screen.findByText('STALE · 오래된 결과')).toBeTruthy();
    expect(screen.getByText('UNKNOWN · 연결 확인 불가')).toBeTruthy();
    expect(screen.getByText('계산 실패')).toBeTruthy();
    // 카메라별 상태·안내는 오른쪽 카메라 패널 행에 보인다.
    const cameraPanel = within(
      screen.getByRole('heading', { name: '카메라 연결 · 방향 안내' }).closest('[data-slot=card]')!,
    );
    expect(cameraPanel.getByText('영상 없음')).toBeTruthy();
    expect(
      cameraPanel.getByText('cam2 · cam2–cam3 공통 시야 없음 — 각도를 조정해 주세요'),
    ).toBeTruthy();

    const proceedButton = screen.getByRole('button', { name: '그대로 진행' }) as HTMLButtonElement;
    await waitFor(() => expect(proceedButton.disabled).toBe(false));

    // 서버 오류(계산 실패)여도 그대로 진행할 수 있다
    cleanup();
    setupMock.state.guideError = true;
    renderSetupPage();

    expect(await screen.findByText('계산 실패')).toBeTruthy();
    const proceedOnFailure = screen.getByRole('button', {
      name: '그대로 진행',
    }) as HTMLButtonElement;
    await waitFor(() => expect(proceedOnFailure.disabled).toBe(false));
    fireEvent.click(proceedOnFailure);

    expect(await screen.findByText(/세션 019a0000/)).toBeTruthy();
  });
});

const POLL_WAIT = { timeout: 8_000 };

/** cam4 높이를 채워 세팅을 시작하고 대기 화면(2단계)의 세션 id 를 돌려준다. */
async function startSetupSession() {
  const view = renderSetupPage();
  const cam1Input = (await screen.findByLabelText('cam1 높이 (m)')) as HTMLInputElement;
  await waitFor(() => expect(cam1Input.value).toBe('2.25'));
  fireEvent.change(screen.getByLabelText('cam4 높이 (m)'), { target: { value: '2.35' } });
  fireEvent.click(screen.getByRole('button', { name: '그대로 진행' }));
  await waitFor(() => expect(setupMock.sessions.size).toBe(1));
  const sessionId = [...setupMock.sessions.keys()][0]!;
  expect(await screen.findByText(`세션 ${sessionId}`)).toBeTruthy();
  return { ...view, sessionId };
}

const PREVIEW_PATCH = {
  stage: 'PREVIEW_AND_VLM_DRAFT',
  status: 'PREVIEW_AND_VLM_DRAFT',
  progress_pct: 92,
  preview_scene_version: 'scene-preview-1',
  calibration_version: 'cal-8',
} as const;

describe('설치 · 보정 2단계 이후(대기 화면·미리보기 맵·AI 초안·발행 뒤)', () => {
  it('대기 화면에 4단계 중 현재 단계·진행률·남은 시간(목표 90초)을 2초 주기로 갱신해 보인다', async () => {
    const { sessionId } = await startSetupSession();

    expect(screen.getByRole('list', { name: '세팅 대기 세부 단계' })).toBeTruthy();
    expect(
      screen.getByRole('progressbar', { name: '세팅 진행률' }).getAttribute('aria-valuenow'),
    ).toBe('0');
    expect(screen.getByLabelText('세팅 진행률 및 남은 시간').textContent).toContain('약 1분 30초');

    setupMock.updateSession(sessionId, {
      stage: 'INITIAL_POSE_AND_WORLD',
      status: 'INITIAL_POSE_AND_WORLD',
      progress_pct: 40,
    });

    await waitFor(
      () =>
        expect(
          screen.getByRole('progressbar', { name: '세팅 진행률' }).getAttribute('aria-valuenow'),
        ).toBe('40'),
      POLL_WAIT,
    );
    expect(screen.getByLabelText('세팅 진행률 및 남은 시간').textContent).toContain('약 54초');
    const steps = within(screen.getByRole('list', { name: '세팅 대기 세부 단계' })).getAllByRole(
      'listitem',
    );
    expect(steps).toHaveLength(4);
    expect(steps[0]!.textContent).toContain('완료');
    expect(steps[1]!.textContent).toContain('진행 중');
    expect(steps[3]!.textContent).toContain('대기');
  }, 20_000);

  it('대기 화면의 세팅 취소는 확인 창 뒤 세션을 취소하고 실패 안내로 바뀐다', async () => {
    const { sessionId } = await startSetupSession();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    fireEvent.click(await screen.findByRole('button', { name: '세팅 취소' }));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await screen.findByRole('alert', { name: '세팅 실패 안내' }, POLL_WAIT);
    expect(screen.getByRole('alert', { name: '세팅 실패 안내' }).textContent).toContain(
      'CANCELLED_BY_ADMIN',
    );
    expect(sessionId).toBeTruthy();
    confirmSpy.mockRestore();
  }, 20_000);

  it('실패하면 사유와 다시 시도를 보이고, 다시 시도하면 retry 요청 뒤 새 세션을 추적한다', async () => {
    const { sessionId } = await startSetupSession();

    setupMock.updateSession(sessionId, {
      stage: 'FAILED',
      status: 'FAILED',
      progress_pct: 45,
      failure_code: 'RECONSTRUCT_FAILED',
      failure_message: '3D 공간 복원에 실패했습니다(시험)',
    });

    const alert = await screen.findByRole('alert', { name: '세팅 실패 안내' }, POLL_WAIT);
    expect(alert.textContent).toContain('RECONSTRUCT_FAILED');
    expect(alert.textContent).toContain('3D 공간 복원에 실패했습니다(시험)');

    fireEvent.click(within(alert).getByRole('button', { name: '다시 시도' }));

    await waitFor(() => expect(setupMock.retryRequests).toHaveLength(1));
    const retried = setupMock.retryRequests[0]!;
    expect(retried.sessionId).toBe(sessionId);
    expect(await screen.findByText(`세션 ${retried.retriedSessionId}`)).toBeTruthy();
    expect(screen.queryByRole('alert', { name: '세팅 실패 안내' })).toBeNull();
    expect(
      screen.getByRole('progressbar', { name: '세팅 진행률' }).getAttribute('aria-valuenow'),
    ).toBe('0');
  }, 20_000);

  it('높이 불일치 경고는 어느 카메라인지 보이고, 1단계 높이 칸으로 돌아가 고쳐 재시도한다', async () => {
    const { sessionId } = await startSetupSession();

    setupMock.updateSession(sessionId, {
      stage: 'JOINT_BA_AND_VERIFY',
      status: 'JOINT_BA_AND_VERIFY',
      progress_pct: 62,
      height_checks: {
        cam1: {
          input_height_m: 2.25,
          restored_height_m: 2.26,
          difference_cm: 1,
          within_tolerance: true,
          guidance: null,
        },
        cam2: {
          input_height_m: 2.3,
          restored_height_m: 2.71,
          difference_cm: 41,
          within_tolerance: false,
          guidance: 'cam2 높이를 다시 재서 입력해 주세요',
        },
      },
    });

    const warning = await screen.findByRole(
      'alert',
      { name: '카메라 높이 불일치 경고' },
      POLL_WAIT,
    );
    expect(warning.textContent).toContain('cam2');
    expect(warning.textContent).toContain('cam2 높이를 다시 재서 입력해 주세요');

    fireEvent.click(within(warning).getByRole('button', { name: '1단계 높이 수정' }));

    const cam2Input = (await screen.findByLabelText('cam2 높이 (m)')) as HTMLInputElement;
    expect(cam2Input.value).toBe('2.3');
    expect(
      screen.getByRole('alert', { name: '카메라 높이 불일치 수정 안내' }).textContent,
    ).toContain('cam2');

    fireEvent.change(cam2Input, { target: { value: '2.7' } });
    fireEvent.click(screen.getByRole('button', { name: '다시 시도' }));

    await waitFor(() => expect(setupMock.retryRequests).toHaveLength(1));
    expect(setupMock.retryRequests[0]!.sessionId).toBe(sessionId);
    expect(setupMock.retryRequests[0]!.body.camera_heights_m?.cam2).toBe(2.7);
    expect(
      await screen.findByText(`세션 ${setupMock.retryRequests[0]!.retriedSessionId}`),
    ).toBeTruthy();
    expect(screen.getByRole('list', { name: '세팅 대기 세부 단계' })).toBeTruthy();
  }, 20_000);

  it('미리보기 맵이 도착하면 구역 편집 도구로 넘어가고, 정식 Scene 이 오면 자동 교체해도 수락해 만든 구역이 남는다', async () => {
    const { sessionId, client } = await startSetupSession();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    expect(screen.queryByTestId('zones-page-stub')).toBeNull();

    setupMock.updateSession(sessionId, { ...PREVIEW_PATCH });

    expect(await screen.findByTestId('zones-page-stub', undefined, POLL_WAIT)).toBeTruthy();
    expect(screen.getByRole('status', { name: '배경 맵 단계 상태' }).textContent).toContain(
      '미리보기 맵',
    );
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['scenes', 'current'] }),
    );

    fireEvent.click(await screen.findByRole('button', { name: '창가 개구부 추락 주의 수락' }));
    expect(await screen.findByText('반영됨', undefined, POLL_WAIT)).toBeTruthy();

    invalidate.mockClear();
    setupMock.updateSession(sessionId, {
      stage: 'COMPLETED',
      status: 'COMPLETED',
      progress_pct: 100,
      scene_version: 'scene-final-1',
    });

    await waitFor(
      () =>
        expect(screen.getByRole('status', { name: '배경 맵 단계 상태' }).textContent).toContain(
          '정밀 3D 맵',
        ),
      POLL_WAIT,
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['scenes', 'current'] });
    expect(screen.getByTestId('zones-page-stub')).toBeTruthy();
    expect(screen.getByText('반영됨')).toBeTruthy();
  }, 25_000);

  it('구역 단계는 왼쪽 3D 와 오른쪽 구역 편집 도구(옆 칸)가 같은 선택을 쓴다: 패널에서 고르면 3D 가 고르고 초점을 옮기고, 3D 에서 고르면 패널이 따른다', async () => {
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, { ...PREVIEW_PATCH });

    const panel = await screen.findByTestId('zones-page-stub', undefined, POLL_WAIT);
    expect(panel.getAttribute('data-placement')).toBe('inline');
    // 3D 코드는 구역 단계에 와서야 받는다(무거운 묶음이라 기다림을 넉넉히 둔다).
    expect(await screen.findByTestId('viewer3d', undefined, POLL_WAIT)).toBeTruthy();
    await waitFor(() => expect(panel.getAttribute('data-viewer-ready')).toBe('true'), POLL_WAIT);
    expect(fakeViewer.props?.layers).toMatchObject({ workSurfaces: 'interactive' });
    expect(screen.getByRole('heading', { name: '미리보기 맵 · scene-preview-1' })).toBeTruthy();

    // 구역 목록을 받은 뒤 패널에서 고르면 3D 가 고르고 초점을 옮긴다.
    await waitFor(() => expect(panel.getAttribute('data-zones')).not.toBe('0'), POLL_WAIT);
    fireEvent.click(within(panel).getByRole('button', { name: 'z-04 고르기' }));
    expect(fakeViewer.select).toHaveBeenCalledWith({ type: 'zone', id: 'z-04' });
    expect(fakeViewer.focus).toHaveBeenCalledWith({ type: 'zone', id: 'z-04' });
    expect(panel.getAttribute('data-selected')).toBe('zone:z-04');

    // 3D 에서 다른 구역을 누르면 패널이 읽는 선택이 바뀐다.
    act(() => fakeViewer.props?.onSelectionChange?.({ type: 'zone', id: 'z-02' }));
    expect(panel.getAttribute('data-selected')).toBe('zone:z-02');
  }, 20_000);

  it('AI 초안은 3D 점선 윤곽으로 겹쳐 보이고, 수락하면 0.3 m 민 구역을 바로 저장하고 버리면 목록·3D 에서 뺀다(AI 의견 없음은 회색)', async () => {
    const commits: unknown[] = [];
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname === '/api/zones/commit') {
        void request
          .clone()
          .json()
          .then((body) => commits.push(body));
      }
    });
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, { ...PREVIEW_PATCH });

    const list = await screen.findByRole('list', { name: 'AI 구역 초안 목록' }, POLL_WAIT);
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]!.getAttribute('data-no-opinion')).toBe('false');
    expect(items[1]!.getAttribute('data-no-opinion')).toBe('true');
    expect(items[1]!.textContent).toContain('AI 의견 없음');
    const [opening, podium] = items.map((item) => item.getAttribute('data-candidate-id')!);
    const ghostIds = () => (fakeViewer.props?.boxGhosts ?? []).map((ghost) => ghost.id);
    await waitFor(
      () => expect(ghostIds()).toEqual([`ai-draft:${opening}`, `ai-draft:${podium}`]),
      POLL_WAIT,
    );

    fireEvent.click(screen.getByRole('button', { name: '창가 개구부 추락 주의 수락' }));
    expect(await within(items[0]!).findByText('반영됨', undefined, POLL_WAIT)).toBeTruthy();
    await waitFor(() => expect(commits).toHaveLength(1));
    expect(commits[0]).toMatchObject({
      zone: {
        op: 'CREATE',
        body: {
          name: '창가 개구부 추락 주의',
          severity: 'CRITICAL',
          polygon_xy: [
            [6.7, 7.7],
            [8.9, 7.7],
            [8.9, 9.1],
            [6.7, 9.1],
          ],
        },
      },
    });
    // 수락한 초안은 구역이 되어 AI 윤곽에서 빠지고, 다시 수락·버릴 단추가 없다.
    expect(ghostIds()).toEqual([`ai-draft:${podium}`]);
    expect(
      within(items[0]!).queryByRole('button', { name: '창가 개구부 추락 주의 수락' }),
    ).toBeNull();
    expect(useZoneEditStore.getState().drafts.__new__).toBeUndefined();

    fireEvent.click(screen.getByRole('button', { name: '전면 단상 작업면 후보 버리기' }));
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(1));
    expect(ghostIds()).toEqual([]);
  }, 20_000);

  it('머리글에는 구역 편집(관제 이동) 단추가 없고, 구역을 발행하면 맨 오른쪽에 작업 관리로 하나만 생긴다', async () => {
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, { ...PREVIEW_PATCH });
    await screen.findByTestId('zones-page-stub', undefined, POLL_WAIT);

    const steps = within(screen.getByRole('list', { name: '보정 단계' })).getAllByRole('button');
    expect(steps).toHaveLength(3);
    expect(steps[2]!.textContent).toBe('3구역');
    expect(steps[2]!.getAttribute('aria-current')).toBe('step');
    expect(screen.queryByRole('button', { name: '구역 편집' })).toBeNull();
    expect(screen.queryByRole('button', { name: '작업 관리로' })).toBeNull();

    act(() => markSetupZonesCommitted());

    const restart = screen.getByRole('button', { name: '다시 보정' });
    const toWork = screen.getByRole('button', { name: '작업 관리로' });
    expect(screen.getAllByRole('button', { name: /작업 관리/ })).toHaveLength(1);
    expect(restart.nextElementSibling).toBe(toWork);
    expect(toWork.nextElementSibling).toBeNull();
    fireEvent.click(toWork);
    expect(navigateMock).toHaveBeenCalledWith({ to: '/work', search: { workUnitId: '1' } });
  }, 20_000);

  it('작업 관리에서 다시 열면(이 브라우저에 세션이 남은 채) 맵이 준비된 세션은 곧바로 구역 단계로 연다', async () => {
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, { ...PREVIEW_PATCH });
    await screen.findByTestId('zones-page-stub', undefined, POLL_WAIT);
    cleanup();

    renderSetupPage();
    expect(await screen.findByTestId('zones-page-stub', undefined, POLL_WAIT)).toBeTruthy();
    expect(screen.queryByLabelText('cam1 높이 (m)')).toBeNull();
    expect(setupMock.submittedBodies).toHaveLength(1);
  }, 20_000);

  it('높이 경고가 남았어도 구역을 이미 발행한 세션은 보정 검증에 머물지 않고 구역 단계로 연다', async () => {
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, {
      ...PREVIEW_PATCH,
      height_checks: {
        cam1: {
          input_height_m: 2.44,
          restored_height_m: 2.36,
          difference_cm: -8,
          within_tolerance: false,
          guidance: 'cam1 높이가 8 cm 안 맞음',
        },
      },
      zones_committed_at: 1_793_848_135_600,
    });

    expect(await screen.findByTestId('zones-page-stub', undefined, POLL_WAIT)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '보정 확정' })).toBeNull();
    expect(screen.getByRole('button', { name: '작업 관리로' })).toBeTruthy();
  }, 20_000);

  it('이 브라우저에 남은 세션이 다른 작업의 것이면 버리고 그 작업의 세팅을 처음부터 연다', async () => {
    await startSetupSession();
    cleanup();

    renderSetupPage(5);
    expect(await screen.findByLabelText('cam1 높이 (m)', undefined, POLL_WAIT)).toBeTruthy();
    await waitFor(() => expect(useSetupFlowStore.getState().activeSessionId).toBeNull());
    expect(screen.queryByTestId('zones-page-stub')).toBeNull();
  }, 20_000);
});
