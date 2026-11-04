import { useCctvStore } from '@/features/cctv/cctv.store';
import { requestCameraHeightDefaults } from '@/features/setup/api';
import {
  markSetupZonesCommitted,
  resetSetupFlowStore,
  useSetupFlowStore,
} from '@/features/setup/setup.store';
import { SetupPage } from '@/features/setup/SetupPage';
import { api } from '@/lib/api';
import { createRulesMock } from '@/mocks/rules';
import { buildCoverageGuidePayload, createSetupMock } from '@/mocks/setup';
import { createZonesMock } from '@/mocks/zones';
import { useZoneEditStore } from '@/store/zoneEdit.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return {
    api: createHttp({ baseUrl: 'http://api.invalid', getToken: () => 'mock-token.foreman' }),
  };
});
const navigateMock = vi.hoisted(() => vi.fn());
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => navigateMock }));
vi.mock('@/features/zones/ZonesPage', () => ({
  ZonesPage: () => <div data-testid="zones-page-stub">구역 편집 도구(-456 재사용)</div>,
}));
vi.mock('@/features/cctv/useCctvPreview', () => ({
  useCctvPreview: () => ({ attach: vi.fn(), retry: vi.fn() }),
}));

let setupMock = createSetupMock();
const rulesMock = createRulesMock({ profile: 'CLASSROOM_TEST' });
const zonesMock = createZonesMock();

const server = setupServer(...rulesMock.handlers, ...zonesMock.handlers, ...setupMock.handlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
beforeEach(() => {
  useCctvStore.getState().reset();
  resetSetupFlowStore();
  useZoneEditStore.getState().clear();
  navigateMock.mockReset();
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
  server.resetHandlers(...rulesMock.handlers, ...zonesMock.handlers, ...setupMock.handlers);
});
afterEach(cleanup);
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

describe('설치 · 보정 1단계 화면', () => {
  it('보정 단계 아래에 규칙 현황과 실시간 CCTV 4칸을 보인다(관리자가 아닌 역할도)', async () => {
    renderSetupPage();
    expect(await screen.findByRole('table')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '규칙' })).toBeTruthy();
    expect(screen.getByText('시험 프로파일')).toBeTruthy();
    expect(screen.getByRole('list', { name: '보정 단계' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: '실시간 CCTV 4칸' })).toBeTruthy();
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
    expect(screen.getAllByText('충분').length).toBeGreaterThan(0);
    expect(screen.getAllByText('약함').length).toBeGreaterThan(0);
    expect(screen.getAllByText('부족').length).toBeGreaterThan(0);

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
    server.resetHandlers(...rulesMock.handlers, ...zonesMock.handlers, ...setupMock.handlers);

    renderSetupPage();

    expect(await screen.findByText('STALE · 오래된 결과')).toBeTruthy();
    expect(screen.getByText('UNKNOWN · 연결 확인 불가')).toBeTruthy();
    expect(screen.getByText('계산 실패')).toBeTruthy();
    expect(screen.getByText(/영상 없음: cam2/)).toBeTruthy();

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

  it('미리보기 맵이 도착하면 구역 편집 도구로 넘어가고, 정식 Scene 이 오면 자동 교체해도 수락한 초안이 남는다', async () => {
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
    expect(useZoneEditStore.getState().drafts.__new__).toBeTruthy();

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
    expect(useZoneEditStore.getState().drafts.__new__).toBeTruthy();
    expect(screen.getByText('초안 반영됨 (발행 전)')).toBeTruthy();
  }, 25_000);

  it('AI 초안은 점선 다각형으로 겹쳐 보이고, 수락하면 편집 초안으로 옮기고 버리면 목록에서 뺀다(AI 의견 없음은 회색)', async () => {
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, { ...PREVIEW_PATCH });

    const list = await screen.findByRole('list', { name: 'AI 구역 초안 목록' }, POLL_WAIT);
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]!.getAttribute('data-no-opinion')).toBe('false');
    expect(items[1]!.getAttribute('data-no-opinion')).toBe('true');
    expect(items[1]!.textContent).toContain('AI 의견 없음');
    expect(screen.getByLabelText('AI 구역 초안 다각형')).toBeTruthy();
    expect(screen.getByLabelText('AI 초안 창가 개구부 추락 주의')).toBeTruthy();
    expect(screen.getByLabelText('AI 초안 전면 단상 작업면 후보 (AI 의견 없음)')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '창가 개구부 추락 주의 수락' }));
    const draft = useZoneEditStore.getState().drafts.__new__;
    expect(draft?.patch).toEqual({ name: '창가 개구부 추락 주의', severity: 'CRITICAL' });
    expect(draft?.box).toEqual({ min: [7, 8, 0], max: [8.6, 8.8, 2] });
    expect(within(items[0]!).getByText('초안 반영됨 (발행 전)')).toBeTruthy();
    expect(zonesMock).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '전면 단상 작업면 후보 버리기' }));
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(1));
    expect(screen.queryByLabelText('AI 초안 전면 단상 작업면 후보 (AI 의견 없음)')).toBeNull();
    expect(screen.getByLabelText('AI 초안 창가 개구부 추락 주의')).toBeTruthy();
  }, 20_000);

  it('구역 발행이 끝나면 작업 관리(TBM) 화면으로 이동하는 버튼을 보인다', async () => {
    const { sessionId } = await startSetupSession();
    setupMock.updateSession(sessionId, { ...PREVIEW_PATCH });
    await screen.findByTestId('zones-page-stub', undefined, POLL_WAIT);

    expect(screen.queryByRole('button', { name: '작업 관리(TBM)로 이동' })).toBeNull();

    act(() => markSetupZonesCommitted());

    expect(screen.getByRole('status', { name: '구역 발행 완료 안내' })).toBeTruthy();
    const buttons = screen.getAllByRole('button', { name: '작업 관리(TBM)로 이동' });
    expect(buttons.length).toBeGreaterThan(0);
    fireEvent.click(buttons[0]!);
    expect(navigateMock).toHaveBeenCalledWith({ to: '/work', search: { workUnitId: '1' } });
  }, 20_000);
});
