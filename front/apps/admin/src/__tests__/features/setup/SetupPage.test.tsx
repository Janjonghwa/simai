import { useCctvStore } from '@/features/cctv/cctv.store';
import { requestCameraHeightDefaults } from '@/features/setup/api';
import { SetupPage } from '@/features/setup/SetupPage';
import { api } from '@/lib/api';
import { createRulesMock } from '@/mocks/rules';
import { buildCoverageGuidePayload, createSetupMock } from '@/mocks/setup';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return {
    api: createHttp({ baseUrl: 'http://api.invalid', getToken: () => 'mock-token.foreman' }),
  };
});
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => vi.fn() }));
vi.mock('@/features/cctv/useCctvPreview', () => ({
  useCctvPreview: () => ({ attach: vi.fn(), retry: vi.fn() }),
}));

let setupMock = createSetupMock();
const rulesMock = createRulesMock({ profile: 'CLASSROOM_TEST' });

const server = setupServer(...rulesMock.handlers, ...setupMock.handlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
beforeEach(() => {
  useCctvStore.getState().reset();
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
  server.resetHandlers(...rulesMock.handlers, ...setupMock.handlers);
});
afterEach(cleanup);
afterAll(() => server.close());

function renderSetupPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <SetupPage />
      </QueryClientProvider>,
    ),
  };
}

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
    server.resetHandlers(...rulesMock.handlers, ...setupMock.handlers);

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
