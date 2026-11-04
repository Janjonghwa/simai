import { SpatialViewportHost } from '@/features/layout/SpatialViewportHost';
import type { Viewer3DProps } from '@/features/viewer3d';
import { createQueryClient } from '@/lib/queryClient';
import { handlers } from '@/mocks/handlers';
import {
  resetMockSceneState,
  setMockSceneQuality,
  setMockSceneRegenerateView,
} from '@/mocks/scenes';
import { useSessionStore } from '@/store/session.store';
import { QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { useLayoutEffect } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/env', () => ({
  env: {
    VITE_API_BASE_URL: 'https://scene-regen.invalid',
    VITE_STOMP_URL: 'wss://scene-regen.invalid/ws',
    VITE_CCTV_PREVIEW_URL: 'wss://scene-regen.invalid/ws/cctv',
    VITE_API_MOCK: false,
  },
}));

let currentMockToken = 'mock-token.admin';
vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return {
    api: createHttp({
      baseUrl: 'https://scene-regen.invalid',
      getToken: () => currentMockToken,
    }),
  };
});

vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: [], loaded: true, failed: false }),
}));

const ZONES_RAW = { contract: 'DEMO_UNCONFIRMED', zones: [] };
vi.mock('@/features/zones/useZones', () => ({
  useZones: () => ({ zones: [], raw: ZONES_RAW, failed: false }),
}));

const viewer = { props: null as Viewer3DProps | null };
function FakeViewer(props: Viewer3DProps) {
  const { onStatusChange } = props;
  useLayoutEffect(() => {
    viewer.props = props;
  });
  useLayoutEffect(() => {
    onStatusChange?.({ phase: 'ready', mode: 'LIVE', error: null });
  }, [onStatusChange]);
  return <div data-testid="viewer3d" />;
}
const loadViewer = () => Promise.resolve({ default: FakeViewer });

const server = setupServer(...handlers);
const clients: ReturnType<typeof createQueryClient>[] = [];

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

beforeEach(() => {
  resetMockSceneState();
  currentMockToken = 'mock-token.admin';
  useSessionStore.setState({
    session: {
      accessToken: 'mock-token.admin',
      expiresAt: Date.now() + 300_000,
      user: { id: '1', role: 'ADMIN' },
    },
  });
});

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  server.resetHandlers();
  resetMockSceneState();
  viewer.props = null;
  vi.useRealTimers();
});

function renderMonitorHost() {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <SpatialViewportHost loadViewer={loadViewer} eventPanel={true} />
    </QueryClientProvider>,
  );
}

describe('실시간 모니터링 화면의 맵 재생성 관리자 도구 (S15P31A101-428)', () => {
  it('VIEWER 역할에게는 맵 재생성 도구를 노출하지 않는다', async () => {
    currentMockToken = 'mock-token.viewer';
    useSessionStore.setState({
      session: {
        accessToken: 'mock-token.viewer',
        expiresAt: Date.now() + 300_000,
        user: { id: '2', role: 'VIEWER' },
      },
    });
    renderMonitorHost();
    await screen.findByTestId('viewer3d');
    expect(screen.queryByTestId('scene-regenerate-panel')).toBeNull();
  });

  it('ADMIN 역할이 사유와 함께 맵 재생성을 요청하면 2초 폴링으로 단계가 전이되고 후보를 활성화할 수 있다', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    resetMockSceneState({ autoAdvanceOnPoll: true });

    renderMonitorHost();
    await waitFor(() => expect(viewer.props?.scene?.ok).toBe(true));

    const input = screen.getByLabelText('재생성 사유');
    fireEvent.change(input, { target: { value: '자재 반입' } });
    fireEvent.click(screen.getByRole('button', { name: '맵 재생성' }));

    // 1. 요청 직후: REQUESTED ('요청 대기 중')
    await waitFor(() =>
      expect(screen.getByTestId('scene-regenerate-status').textContent).toContain('요청 대기 중'),
    );

    // 2. 첫 2초 폴링: CAPTURING ('배경 수집 중')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    await waitFor(() =>
      expect(screen.getByTestId('scene-regenerate-status').textContent).toContain('배경 수집 중'),
    );

    // 3. 다음 2초 폴링: RECONSTRUCTING ('3D 재구성 중')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    await waitFor(() =>
      expect(screen.getByTestId('scene-regenerate-status').textContent).toContain('3D 재구성 중'),
    );

    // 4. 다음 2초 폴링: CANDIDATE_READY ('새 3D 배경 후보 준비됨') + 활성화 버튼 노출
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    await waitFor(() =>
      expect(screen.getByTestId('scene-regenerate-status').textContent).toContain(
        '새 3D 배경 후보 준비됨',
      ),
    );
    const activateBtn = screen.getByRole('button', { name: '새 3D 배경 후보 — 활성화' });
    expect(activateBtn).toBeTruthy();

    // 5. 활성화 클릭 시 POST /api/scenes/{sceneVersion}/commit 호출 후 현재 Scene 재조회
    fireEvent.click(activateBtn);
    await waitFor(() =>
      expect(viewer.props?.scene).toMatchObject({
        ok: true,
        raw: {
          scene_version: 'scene-cand-1',
          state: 'COMMITTED',
        },
      }),
    );
    expect(screen.getByText(/새 3D 배경 활성화됨/)).toBeTruthy();
  });

  it('이미 진행 중인 요청이 있을 때(409 REGENERATE_IN_PROGRESS) 한국어 안내와 함께 해당 요청 상태를 표시한다', async () => {
    renderMonitorHost();
    await screen.findByTestId('viewer3d');

    // 다른 관리자가 요청해 이미 CAPTURING 진행 중인 상태를 주입
    setMockSceneRegenerateView({
      request_id: 7,
      status: 'CAPTURING',
      failure_reason: null,
      scene_version: null,
      reason: '크레인 이동',
      requested_at_ms: Date.now() - 5000,
      updated_at_ms: Date.now(),
    });

    fireEvent.click(screen.getByRole('button', { name: '맵 재생성' }));

    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toContain(
        '이미 진행 중인 맵 재생성 요청이 있습니다.',
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId('scene-regenerate-status').textContent).toContain('배경 수집 중'),
    );
  });

  it('재생성 실패 시 한국어 실패 사유를 표시한다', async () => {
    setMockSceneRegenerateView({
      request_id: 9,
      status: 'FAILED',
      failure_reason: 'BACKGROUND_NOT_READY',
      scene_version: null,
      reason: null,
      requested_at_ms: Date.now() - 10_000,
      updated_at_ms: Date.now(),
    });

    renderMonitorHost();
    await waitFor(() =>
      expect(screen.getByTestId('scene-regenerate-status').textContent).toContain('재생성 실패'),
    );
    expect(screen.getByTestId('scene-regenerate-failure-reason').textContent).toContain(
      '사람 없는 배경 프레임이 아직 준비되지 않았습니다.',
    );
  });

  describe('재생성 품질 표시 (S15P31A101-469)', () => {
    it('quality 가 없으면 아무것도 표시하지 않는다', async () => {
      renderMonitorHost();
      await screen.findByTestId('scene-regenerate-panel');
      await new Promise((r) => setTimeout(r, 50));
      expect(screen.queryByTestId('scene-regenerate-quality')).toBeNull();
    });

    it('채움·비움 비율과 카메라별 값을 보여 주고 30% 미만이면 안내하지 않는다', async () => {
      setMockSceneQuality({
        previous_fill_ratio: 0.12,
        unrevealed_ratio: 0.05,
        camera_background_quality: {
          'cam-a': { previous_fill_ratio: 0.1, unrevealed_ratio: 0.02 },
        },
      });
      renderMonitorHost();
      const q = await screen.findByTestId('scene-regenerate-quality');
      expect(q.textContent).toContain('이전 모습으로 채움 12% · 비움 5%');
      expect(screen.getByTestId('scene-regenerate-quality-cameras').textContent).toContain(
        'cam-a 채움 10%/비움 2%',
      );
      expect(screen.queryByTestId('scene-regenerate-quality-notice')).toBeNull();
    });

    it('채움이 30% 이상이면 일부는 이전 모습이라는 안내를 보여 준다', async () => {
      setMockSceneQuality({ previous_fill_ratio: 0.3, unrevealed_ratio: 0.1 });
      renderMonitorHost();
      const notice = await screen.findByTestId('scene-regenerate-quality-notice');
      expect(notice.textContent).toContain('일부는 이전 모습입니다');
    });
  });
});
