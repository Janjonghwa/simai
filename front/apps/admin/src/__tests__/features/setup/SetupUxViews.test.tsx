import { useCctvStore } from '@/features/cctv/cctv.store';
import { deriveCaptureView } from '@/features/setup/backgroundCapture';
import { BackgroundCaptureModal } from '@/features/setup/BackgroundCaptureModal';
import { deriveCameraCheckView, deriveCameraWalkView } from '@/features/setup/cameraCheck';
import { CameraCheckModal } from '@/features/setup/CameraCheckModal';
import { calibrateNextStep } from '@/features/setup/nextStep';
import { deriveWaitingView } from '@/features/setup/setup';
import { SetupCalibrateStep } from '@/features/setup/SetupCalibrateStep';
import { AiZoneDraftsPanel } from '@/features/setup/SetupPanels';
import { SetupZoneReviewCard } from '@/features/setup/SetupZoneReviewCard';
import { setupSessionResponseSchema, setupZoneDraftsResponseSchema } from '@/features/setup/types';
import { useBackgroundCapture } from '@/features/setup/useBackgroundCapture';
import type { Zone } from '@/features/zones/types';
import { zoneAiOpinionSchema, zoneHeightChangeSchema } from '@/features/zones/types';
import { readZones } from '@/features/zones/zones';
import { buildCameraCheck, buildSetupZoneDraftsResponse } from '@/mocks/setup';
import { MOCK_ZONES } from '@/mocks/zones';
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  within,
} from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', () => ({ api: vi.fn() }));
vi.mock('@/features/cctv/useCctvPreview', () => ({ useCctvPreview: () => ({ attach: vi.fn() }) }));
const viewport = vi.hoisted(() => ({
  zones: [] as Zone[],
  selectTarget: vi.fn(),
  clearSelection: vi.fn(),
}));
vi.mock('@/features/layout/SpatialViewportHost', () => ({ useSpatialViewport: () => viewport }));
vi.mock('@/features/setup/useSetupZoneReview', () => ({
  useSetupZoneReview: () => ({
    pending: 1,
    unchangedPending: 0,
    busy: false,
    confirm: vi.fn(),
    confirmAll: vi.fn(),
    confirmUnchanged: vi.fn(),
    remove: vi.fn(),
  }),
}));

const IDS = ['cam_A', 'cam_B', 'cam_C', 'cam_D'];
const session = (patch: Record<string, unknown> = {}) =>
  setupSessionResponseSchema.parse({
    setup_session_id: 's1',
    site_id: 's',
    work_unit_id: 1,
    status: 'COLLECTING_BACKGROUND',
    stage: 'COLLECTING_BACKGROUND',
    progress_pct: 0,
    camera_heights_m: Object.fromEntries(IDS.map((id) => [id, 2.4])),
    started_at_ms: 1000,
    updated_at_ms: 1000,
    ...patch,
  });
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useCctvStore.getState().reset();
});

describe('세팅 상태와 다음 행동(-732)', () => {
  it('맵 완료 뒤 걷기를 재요청할 수 없으면 이유와 카메라 연결 관리로 가는 단추를 한 번만 보인다', () => {
    const current = session({
      status: 'COMPLETED',
      stage: 'COMPLETED',
      progress_pct: 100,
      scene_version: 'scene-final',
      camera_check_passed: false,
    });
    const waiting = deriveWaitingView(current);
    const check = deriveCameraCheckView(current, IDS);
    const onReturn = vi.fn();
    render(
      <SetupCalibrateStep
        waiting={waiting}
        cameraView={<div />}
        nextStep={calibrateNextStep({
          waiting,
          pollingError: false,
          walkOpen: false,
          cameraCheckBlocked: '카메라 확인을 마치면 넘어갈 수 있어요',
        })}
        cameraCheck={check}
        onRetry={vi.fn()}
        onReturnToStep1={onReturn}
      />,
    );
    expect(screen.getAllByText('맵은 완성됐지만 카메라 확인이 남아 있어요')).toHaveLength(1);
    expect(screen.getByText(/맵 생성 작업이 끝나 걷기만 다시 할 수 없어요/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '카메라 확인만 다시' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '카메라 연결 관리로' }));
    expect(onReturn).toHaveBeenCalledOnce();
  });

  it('서버 접수 전 경과를 세되 촬영 카운트다운은 접수 뒤부터 센다', () => {
    vi.useFakeTimers();
    vi.setSystemTime(11000);
    const initial = session();
    const hook = renderHook(
      ({ current }) =>
        useBackgroundCapture({ stage: 1, session: current, cameraIds: IDS, starting: false }),
      { initialProps: { current: initial } },
    );
    expect(hook.result.current.waitingForWorker).toBe(true);
    act(() => vi.advanceTimersByTime(3000));
    expect(hook.result.current.waitingSeconds).toBe(13);
    expect(hook.result.current.secondsLeft).toBe(20);
    hook.rerender({
      current: session({
        background_capture: {
          since_ms: 14000,
          required_samples: 12,
          cameras: Object.fromEntries(
            IDS.map((id) => [id, { state: 'INSUFFICIENT_SAMPLES', sample_count: 1 }]),
          ),
        },
      }),
    });
    expect(hook.result.current.waitingForWorker).toBe(false);
    act(() => vi.advanceTimersByTime(1500));
    expect(hook.result.current.secondsLeft).toBe(19);
  });

  it('배경 촬영을 받기 전에는 비킬 필요 없음과 경과를 표시하고 미리보기 중지만 구분한다', () => {
    useCctvStore.getState().setLink('stopped');
    render(
      <BackgroundCaptureModal
        view={deriveCaptureView(session(), IDS, false)}
        secondsLeft={20}
        waitingForWorker
        waitingSeconds={93}
        onStart={vi.fn()}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    const dialog = screen.getByRole('dialog', { name: '배경 촬영' });
    expect(dialog.textContent).toContain(
      '서버 준비 기다리는 중 · 아직 비키지 않아도 됩니다 · 93초 경과',
    );
    expect(dialog.textContent).not.toContain('화면 밖에서 기다려');
    expect(within(dialog).getAllByText('미리보기 멈춤(촬영 계속)')).toHaveLength(4);
    expect(within(dialog).queryByLabelText('남은 시간')).toBeNull();
  });

  it('카메라가 각각 충분히 보여도 함께 보인 횟수가 모자란 쌍을 강조한다', () => {
    const check = buildCameraCheck(IDS, 'cal', 'MEASURING', 50);
    for (const camera of Object.values(check.cameras)) camera.observed_views = 100;
    const pair = Object.values(check.pairs).find((p) => p.required)!;
    pair.samples = 7;
    const view = deriveCameraWalkView(
      session({
        camera_check_phase: 'WALKING',
        camera_check: { ...check, thresholds: { min_pair_samples: 30 } },
      }),
      IDS,
    );
    render(<CameraCheckModal view={view} secondsLeft={5} onStart={vi.fn()} onStop={vi.fn()} />);
    const list = screen.getByRole('list', { name: '카메라 쌍별 함께 보임' });
    const short = within(list).getByText('함께 보임 7/30');
    expect(short.closest('li')?.getAttribute('data-shortage')).toBe('true');
    expect(screen.getAllByText('충분히 보임')).toHaveLength(4);
  });

  it('AI 초안 기본 설명에 수치가 없고 원문·높이·신뢰도는 닫힌 자세히 안에 있다', () => {
    const response = setupZoneDraftsResponseSchema.parse(buildSetupZoneDraftsResponse('s1'));
    render(
      <AiZoneDraftsPanel
        draftsResponse={response}
        acceptedDraftIds={[]}
        discardedDraftIds={[]}
        pendingDraftId={null}
        acceptFailure={null}
        onAcceptDraft={vi.fn()}
        onDiscardDraft={vi.fn()}
        cameraIds={IDS}
      />,
    );
    for (const item of response.drafts) {
      const details = screen.getByRole('group', { name: `${item.recommended_name} 근거 자세히` });
      expect(details.hasAttribute('open')).toBe(false);
      expect(details.textContent).toContain('높이 범위');
      fireEvent.click(within(details).getByText('자세히'));
    }
    expect(screen.getByRole('list', { name: 'AI 구역 초안 목록' }).textContent).not.toContain(
      'cam_',
    );
  });

  it('변화 없음과 AI 수정 권고가 겹치면 변화 감지로 표시하고 사진도 카메라 번호를 쓴다', () => {
    const zone = readZones(MOCK_ZONES)[0]!;
    viewport.zones = [
      {
        ...zone,
        needsReview: true,
        heightChange: zoneHeightChangeSchema.parse({ zone_id: zone.zoneId, status: 'UNCHANGED' }),
        aiOpinion: zoneAiOpinionSchema.parse({
          status: 'OPINION',
          recommended_action: 'MODIFY',
          rationale: 'cam_C 높이가 달라졌습니다.',
          camera_id: 'cam_C',
          before: { image_url: 'before' },
          after: { image_url: 'after' },
        }),
      },
    ];
    render(<SetupZoneReviewCard sessionId="s" cameraIds={IDS} />);
    expect(screen.getByText('변화 감지')).toBeTruthy();
    expect(screen.queryByText('변화 없음')).toBeNull();
    expect(screen.getByAltText('이전 세팅 · 카메라 3')).toBeTruthy();
    expect(screen.getByText('카메라 3 높이가 달라졌습니다.')).toBeTruthy();
  });
});
