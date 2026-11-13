import { deriveCameraCheckView } from '@/features/setup/cameraCheck';
import { deriveWaitingView } from '@/features/setup/setup';
import { SetupCalibrateStep } from '@/features/setup/SetupCalibrateStep';
import { setupSessionResponseSchema } from '@/features/setup/types';
import { buildCameraCheck, buildQualityGate } from '@/mocks/setup';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// 미리보기 3D 칸이 읽는 현재 Scene 조회(@/lib/api → env)는 여기서 쓰지 않는다. 3D 배치는 SetupPage 시험이 본다.
vi.mock('@/lib/api', () => ({ api: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const check = (over: Record<string, unknown> = {}) => ({
  input_height_m: 2.25,
  restored_height_m: 2.26,
  difference_cm: 1,
  within_tolerance: true,
  guidance: null,
  ...over,
});

const sessionOf = (over: Record<string, unknown> = {}) =>
  setupSessionResponseSchema.parse({
    setup_session_id: 's1',
    site_id: 'classroom-1',
    work_unit_id: 1,
    status: 'JOINT_BA_AND_VERIFY',
    stage: 'JOINT_BA_AND_VERIFY',
    progress_pct: 50,
    camera_heights_m: { cam_A: 2.25, cam_B: 2.3 },
    height_tolerance_cm: 5,
    started_at_ms: Date.now(),
    updated_at_ms: Date.now(),
    ...over,
  });

const failedSession = (over: Record<string, unknown> = {}) =>
  sessionOf({ status: 'FAILED', stage: 'FAILED', ...over });

function renderStep(
  session: ReturnType<typeof sessionOf>,
  handlers: { onRetry?: () => void; onReturnToStep1?: () => void; retryPending?: boolean } = {},
) {
  const onRetry = handlers.onRetry ?? vi.fn();
  const onReturnToStep1 = handlers.onReturnToStep1 ?? vi.fn();
  render(
    <SetupCalibrateStep
      cameraView={<div data-testid="camera-view" />}
      waiting={deriveWaitingView(session)}
      onRetry={onRetry}
      retryPending={handlers.retryPending}
      onReturnToStep1={onReturnToStep1}
    />,
  );
  return { onRetry, onReturnToStep1 };
}

const stages = () =>
  within(screen.getByRole('list', { name: '보정·생성 단계' })).getAllByRole('listitem');

describe('보정·생성 진행 카드(5단계 막대)', () => {
  it('5단계를 순서대로 보이고 끝난 단계는 완료, 지금 단계는 구간 안 %, 남은 단계는 대기로 보인다', () => {
    renderStep(sessionOf({ progress_pct: 52.5 }));
    expect(screen.getByRole('heading', { name: '보정·생성' })).toBeTruthy();
    const rows = stages();
    expect(rows.map((row) => row.getAttribute('data-status'))).toEqual([
      'done',
      'done',
      'current',
      'upcoming',
      'upcoming',
    ]);
    expect(rows.map((row) => row.textContent)).toEqual([
      '배경 수집완료',
      '3D 공간·좌표계완료',
      '카메라 정밀 보정진행 중 · 0분 00초 경과50%',
      '미리보기 맵·구역 초안대기',
      '정밀 3D 맵대기',
    ]);
    expect(
      screen
        .getByRole('progressbar', { name: '카메라 정밀 보정 진행률' })
        .getAttribute('aria-valuenow'),
    ).toBe('50');
    expect(
      screen.getByRole('progressbar', { name: '세팅 진행률' }).getAttribute('aria-valuenow'),
    ).toBe('52.5');
    expect(screen.getByText('진행 52.5%')).toBeTruthy();
  });

  it('서버 값이 그대로면 시간이 지나도 막대는 오르지 않고 현재 단계 시간만 흐른다', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-12T00:00:00Z'));
    const start = Date.now();
    const props = (pct: number, updated: number, id = 's1') => ({
      waiting: deriveWaitingView(
        sessionOf({
          setup_session_id: id,
          progress_pct: pct,
          started_at_ms: start - 120_000,
          updated_at_ms: updated,
        }),
      ),
      cameraView: null,
      onRetry: vi.fn(),
      onReturnToStep1: vi.fn(),
    });
    const view = render(<SetupCalibrateStep {...props(50, start - 10_000)} />);
    act(() => vi.advanceTimersByTime(60_000));
    expect(screen.getByText('진행 50%')).toBeTruthy();
    expect(screen.getByText('진행 중 · 1분 10초 경과')).toBeTruthy();
    expect(
      screen
        .getByRole('progressbar', { name: '카메라 정밀 보정 진행률' })
        .getAttribute('aria-valuenow'),
    ).toBe('40');
    // 같은 단계에서 하위 진행을 받아도 단계 시간은 유지한다.
    view.rerender(<SetupCalibrateStep {...props(55, Date.now())} />);
    expect(screen.getByText('진행 55%')).toBeTruthy();
    expect(screen.getByText('진행 중 · 1분 10초 경과')).toBeTruthy();
    view.rerender(<SetupCalibrateStep {...props(67, Date.now())} />);
    expect(screen.getByText('진행 중 · 0분 00초 경과')).toBeTruthy();
    act(() => vi.advanceTimersByTime(5_000));
    expect(screen.getByText('진행 중 · 0분 05초 경과')).toBeTruthy();
    view.rerender(<SetupCalibrateStep {...props(67, Date.now(), 's2')} />);
    expect(screen.getByText('진행 중 · 0분 00초 경과')).toBeTruthy();
  });

  it('배경을 모으는 동안만 비켜 달라는 안내와 카메라별 수집률을 단계 아래에 보인다', () => {
    renderStep(
      sessionOf({
        status: 'COLLECTING_BACKGROUND',
        stage: 'COLLECTING_BACKGROUND',
        progress_pct: 7.5,
        background_collection_pct: { cam_A: 50, cam_B: 80 },
      }),
    );
    expect(stages()[0]!.textContent).toContain('50%');
    expect(screen.getByText('배경을 모으는 동안 카메라 앞에서 비켜 주세요.')).toBeTruthy();
    expect(screen.getByLabelText('카메라별 배경 수집 진행률').textContent).toBe(
      '카메라 1 50% · 카메라 2 80%',
    );
    cleanup();

    renderStep(
      sessionOf({ progress_pct: 20, background_collection_pct: { cam_A: 100, cam_B: 100 } }),
    );
    expect(screen.queryByText('배경을 모으는 동안 카메라 앞에서 비켜 주세요.')).toBeNull();
    expect(screen.queryByLabelText('카메라별 배경 수집 진행률')).toBeNull();
  });

  it('높이 불일치나 결과가 없으면 안내와 결과 표를 그리지 않는다', () => {
    renderStep(sessionOf({ progress_pct: 20 }));
    expect(screen.queryByRole('alert', { name: '카메라 높이 불일치 경고' })).toBeNull();
    expect(screen.queryByRole('table')).toBeNull();
  });
});

describe('보정·생성 실패 안내', () => {
  it('어느 단계에서 멈췄는지(마지막 서버 진행률)·원인·해결을 보이고 그 단계 막대를 실패로 칠한다', () => {
    renderStep(
      failedSession({
        progress_pct: 40,
        failure_code: 'VALIDATION_REJECTED',
        failure_message: "worsened_pairs=['cam_A-cam_B']",
      }),
    );
    const box = screen.getByRole('alert', { name: '세팅 실패 안내' });
    expect(box.textContent).toContain('카메라 정밀 보정 단계에서 멈췄어요');
    expect(box.textContent).toContain(
      '보정한 뒤 오히려 어긋난 카메라 쌍이 있어요 (카메라 1–카메라 2)',
    );
    expect(box.textContent).toContain(
      '카메라와 거치대를 단단히 고정하고 위치를 다시 맞춘 뒤 처음부터 다시 하세요.',
    );
    expect(stages().map((row) => row.getAttribute('data-status'))).toEqual([
      'done',
      'done',
      'failed',
      'upcoming',
      'upcoming',
    ]);
    expect(stages()[2]!.textContent).toBe('카메라 정밀 보정실패');
  });

  it('실패 코드 안내에 서버 코드와 서버 메시지 원문은 보이지 않는다', () => {
    renderStep(
      failedSession({
        progress_pct: 14,
        failure_code: 'BACKGROUND_INCOMPLETE',
        failure_message: 'raw server message',
      }),
    );
    const box = screen.getByRole('alert', { name: '세팅 실패 안내' });
    expect(box.textContent).toContain('배경 수집 단계에서 멈췄어요');
    expect(box.textContent).not.toContain('BACKGROUND_INCOMPLETE');
    expect(box.textContent).not.toContain('raw server message');
  });

  it('같은 상태로 다시 해도 소용없는 실패는 카메라 연결 관리로 단추 하나뿐이다', () => {
    const { onRetry, onReturnToStep1 } = renderStep(
      failedSession({
        progress_pct: 40,
        failure_code: 'INSUFFICIENT_FEATURES',
        failure_message: 'Insufficient raw feature matches on pair cam_A-cam_B: 7',
      }),
    );
    const box = within(screen.getByRole('alert', { name: '세팅 실패 안내' }));
    expect(
      box.getByText('옆 카메라 카메라 1–카메라 2 사이에 같이 보이는 곳이 적어요'),
    ).toBeTruthy();
    expect(box.queryByRole('button', { name: '다시 시도' })).toBeNull();
    expect(box.getAllByRole('button')).toHaveLength(1);
    fireEvent.click(box.getByRole('button', { name: '카메라 연결 관리로' }));
    expect(onReturnToStep1).toHaveBeenCalledTimes(1);
    expect(onRetry).not.toHaveBeenCalled();
  });

  it('다시 해 볼 만한 실패는 다시 시도(주)와 카메라 연결 관리로(보조) 두 단추다', () => {
    const { onRetry, onReturnToStep1 } = renderStep(
      failedSession({ progress_pct: 40, failure_code: 'TIMEOUT' }),
    );
    const box = within(screen.getByRole('alert', { name: '세팅 실패 안내' }));
    expect(box.getAllByRole('button').map((b) => b.textContent)).toEqual([
      '다시 시도',
      '카메라 연결 관리로',
    ]);
    fireEvent.click(box.getByRole('button', { name: '다시 시도' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    fireEvent.click(box.getByRole('button', { name: '카메라 연결 관리로' }));
    expect(onReturnToStep1).toHaveBeenCalledTimes(1);
  });

  it('다시 시도 요청 중에는 다시 시도 단추가 잠긴다', () => {
    renderStep(failedSession({ failure_code: 'TIMEOUT' }), { retryPending: true });
    const retry = screen.getByRole('button', { name: '다시 시도' }) as HTMLButtonElement;
    expect(retry.disabled).toBe(true);
  });

  it('실패 중에는 높이 불일치 안내를 보이지 않는다', () => {
    renderStep(
      failedSession({
        progress_pct: 70,
        failure_code: 'TIMEOUT',
        height_checks: { cam_A: check({ within_tolerance: false, difference_cm: 41 }) },
      }),
    );
    expect(screen.queryByRole('alert', { name: '카메라 높이 불일치 경고' })).toBeNull();
  });
});

describe('보정 검증 결과 표', () => {
  const withChecks = (over: Record<string, unknown> = {}) =>
    sessionOf({
      progress_pct: 70,
      height_checks: {
        cam_A: check(),
        cam_B: check({
          input_height_m: 2.3,
          restored_height_m: 2.71,
          difference_cm: 41,
          within_tolerance: false,
          guidance: 'cam_B 높이를 다시 재서 입력해 주세요',
        }),
      },
      ...over,
    });

  it('카메라·입력·복원·차이 네 열로 카메라마다 한 행을 보이고 카메라 이름은 서버 id 가 아니다', () => {
    renderStep(withChecks());
    const table = screen.getByRole('table');
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((cell) => cell.textContent),
    ).toEqual(['카메라', '입력', '복원', '차이']);
    const rows = within(table).getAllByRole('row').slice(1);
    expect(
      rows.map((row) =>
        within(row)
          .getAllByRole('cell')
          .map((c) => c.textContent),
      ),
    ).toEqual([
      ['2.25 m', '2.26 m', '1.0 cm'],
      ['2.3 m', '2.71 m', '41.0 cm'],
    ]);
    expect(rows.map((row) => within(row).getByRole('rowheader').textContent)).toEqual([
      '카메라 1',
      '카메라 2',
    ]);
    expect(screen.getByRole('heading', { name: '보정 검증 결과' })).toBeTruthy();
    expect(screen.getByText('높이 차이 허용 5 cm')).toBeTruthy();
  });

  it('안 맞는 행은 서버 within_tolerance 로 정해 delay 톤으로 보인다(허용값으로 다시 계산하지 않는다)', () => {
    renderStep(
      withChecks({
        height_checks: {
          cam_A: check({ difference_cm: 0.5, within_tolerance: false }),
          cam_B: check({ difference_cm: 41, within_tolerance: true }),
        },
      }),
    );
    const rows = within(screen.getByRole('table')).getAllByRole('row').slice(1);
    expect(rows[0]!.getAttribute('data-within-tolerance')).toBe('false');
    expect(rows[0]!.className).toContain('text-delay');
    expect(rows[1]!.getAttribute('data-within-tolerance')).toBe('true');
    expect(rows[1]!.className).not.toContain('text-delay');
  });

  it('복원 높이·차이를 서버가 주지 않았으면 줄표로 보이고 허용값이 없으면 허용 문구를 생략한다', () => {
    renderStep(
      withChecks({
        height_tolerance_cm: undefined,
        height_checks: {
          cam_A: check({ restored_height_m: null, difference_cm: null, within_tolerance: false }),
        },
      }),
    );
    const cells = within(screen.getByRole('table')).getAllByRole('cell');
    expect(cells.map((cell) => cell.textContent)).toEqual(['2.25 m', '—', '—']);
    expect(screen.queryByText(/높이 차이 허용/)).toBeNull();
  });

  it('높이 불일치 안내는 서버 문구를 그대로 보이고, 서버가 높이를 확인하기 전(40% 미만)에는 숨긴다', () => {
    renderStep(withChecks());
    expect(screen.getByRole('alert', { name: '카메라 높이 불일치 경고' }).textContent).toContain(
      'cam_B 높이를 다시 재서 입력해 주세요',
    );
    cleanup();
    renderStep(withChecks({ progress_pct: 39 }));
    expect(screen.queryByRole('alert', { name: '카메라 높이 불일치 경고' })).toBeNull();
    // 결과 표는 서버가 값을 준 뒤에는 늘 보인다
    expect(screen.getByRole('table')).toBeTruthy();
  });
});

describe('카메라 확인 카드·쌍별 검증 표(S15P31A101-646)', () => {
  const CAMS = ['cam_A', 'cam_B', 'cam_C', 'cam_D'];
  const readySession = (over: Record<string, unknown> = {}) =>
    // 미리보기 전(3D 칸 없음): 보정 등록(65%) 뒤부터 카메라 확인이 돈다
    sessionOf({
      status: 'JOINT_BA_AND_VERIFY',
      stage: 'JOINT_BA_AND_VERIFY',
      progress_pct: 65,
      camera_heights_m: { cam_A: 2.44, cam_B: 2.44, cam_C: 2.48, cam_D: 2.48 },
      calibration_version: 'cal-9',
      quality_gate: buildQualityGate(CAMS),
      ...over,
    });

  function renderCheck(session: ReturnType<typeof sessionOf>, onRecalibrate = vi.fn()) {
    const waiting = deriveWaitingView(session);
    render(
      <SetupCalibrateStep
        cameraView={<div data-testid="camera-view" />}
        waiting={waiting}
        cameraCheck={deriveCameraCheckView(session, waiting.cameraIds)}
        showCameraCheck
        onRecalibrate={onRecalibrate}
        onRetry={vi.fn()}
        onReturnToStep1={vi.fn()}
      />,
    );
    return onRecalibrate;
  }

  it('재는 중에는 안내와 진행률을 보이고 쌍별 표에 6쌍이 있다', () => {
    renderCheck(
      readySession({
        camera_check: buildCameraCheck(CAMS, 'cal-9', 'MEASURING', 40),
        camera_check_passed: false,
      }),
    );
    const card = screen.getByRole('heading', { name: '카메라 확인' }).closest('[data-slot=card]')!;
    expect(card.textContent).toContain('계속 걸어 주세요');
    expect(
      within(card as HTMLElement)
        .getByRole('progressbar', { name: '카메라 확인 진행률' })
        .getAttribute('aria-valuenow'),
    ).toBe('40');
    const table = screen.getByRole('table', { name: '카메라 쌍별 검증' });
    expect(within(table).getAllByRole('row')).toHaveLength(7);
    expect(screen.queryByRole('button', { name: '다시 보정' })).toBeNull();
  });

  it('실패하면 원인과 다시 보정을 보이고 누르면 다시 보정을 부른다', () => {
    const onRecalibrate = renderCheck(
      readySession({
        camera_check: buildCameraCheck(CAMS, 'cal-9', 'FAILED', 100),
        camera_check_passed: false,
      }),
    );
    expect(screen.getByRole('alert', { name: '카메라 확인 문제' }).textContent).toContain('통과율');
    const failedRow = screen
      .getAllByRole('row')
      .find((row) => row.textContent?.startsWith('카메라 2–카메라 3'))!;
    expect(failedRow.getAttribute('data-outcome')).toBe('fail');
    fireEvent.click(screen.getByRole('button', { name: '다시 보정' }));
    expect(onRecalibrate).toHaveBeenCalledOnce();
  });

  it('통과하면 통과로 보이고 안내가 없다', () => {
    renderCheck(
      readySession({
        camera_check: buildCameraCheck(CAMS, 'cal-9', 'PASSED'),
        camera_check_passed: true,
      }),
    );
    const card = screen.getByRole('heading', { name: '카메라 확인' }).closest('[data-slot=card]')!;
    expect(card.textContent).toContain('통과');
    expect(within(card as HTMLElement).queryByRole('status')).toBeNull();
  });
});
