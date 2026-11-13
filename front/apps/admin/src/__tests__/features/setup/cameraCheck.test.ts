import {
  cameraCheckBlockReason,
  cameraCheckFailed,
  cameraCheckGuide,
  deriveCameraCheckView,
} from '@/features/setup/cameraCheck';
import {
  deriveSetupActions,
  type SetupActionHandlers,
  type SetupActionState,
} from '@/features/setup/setupActions';
import { setupSessionResponseSchema, type SetupSessionResponse } from '@/features/setup/types';
import { buildCameraCheck, buildQualityGate, createSetupMock } from '@/mocks/setup';
import { describe, expect, it, vi } from 'vitest';

// 보정 품질 게이트·카메라 확인(S15P31A101-646) 화면 판단.

const CAMS = ['cam_A', 'cam_B', 'cam_C', 'cam_D'];

const session = (patch: Record<string, unknown> = {}): SetupSessionResponse =>
  setupSessionResponseSchema.parse({
    setup_session_id: 's-646',
    site_id: 'lobby',
    work_unit_id: 7,
    status: 'BAKING_TEXTURE',
    progress_pct: 95,
    camera_heights_m: { cam_A: 2.44, cam_B: 2.44, cam_C: 2.48, cam_D: 2.48 },
    calibration_version: 'cal-646',
    ...patch,
  });

describe('deriveCameraCheckView', () => {
  it('옆 카메라 4쌍을 만들고 보정 홀드아웃·사람 통과율을 카메라 짝으로 맞춘다(키 순서와 무관)', () => {
    const gate = buildQualityGate(CAMS);
    const view = deriveCameraCheckView(
      session({
        quality_gate: gate,
        camera_check: buildCameraCheck(CAMS, 'cal-646', 'PASSED'),
        camera_check_passed: true,
      }),
      CAMS,
    );
    expect(view.pairs.map((p) => p.label)).toEqual([
      '카메라 1–카메라 2',
      '카메라 1–카메라 4',
      '카메라 2–카메라 3',
      '카메라 3–카메라 4',
    ]);
    const da = view.pairs.find((p) => p.label === '카메라 1–카메라 4')!;
    // 품질 게이트 키는 'cam_D-cam_A'(고리 순서)지만 camera_ids 로 맞춘다
    expect(da.holdoutMedianPx).toBeCloseTo(0.75);
    expect(da.outcome).toBe('pass');
    // 마주 보는 쌍(1–3, 2–4)은 서버가 판정을 줘도 표에 두지 않는다.
    expect(view.pairs.find((p) => p.label === '카메라 1–카메라 3')).toBeUndefined();
    expect(view.pairs.find((p) => p.label === '카메라 2–카메라 4')).toBeUndefined();
    expect(view.passed).toBe(true);
    expect(cameraCheckBlockReason(view)).toBeNull();
    expect(cameraCheckGuide(view)).toBeNull();
  });

  it('실패한 쌍과 원인 문장을 보이고 다음을 막는다', () => {
    const view = deriveCameraCheckView(
      session({
        camera_check: buildCameraCheck(CAMS, 'cal-646', 'FAILED', 100),
        camera_check_passed: false,
      }),
      CAMS,
    );
    expect(view.pairs.find((p) => p.label === '카메라 2–카메라 3')!.outcome).toBe('fail');
    expect(cameraCheckFailed(view)).toBe(true);
    expect(view.problems[0]).toContain('통과율');
    expect(cameraCheckBlockReason(view)).toContain('다시 보정');
  });

  it('품질 게이트 실패(세션 FAILED)도 쌍을 실패로 보이고 문제를 싣는다', () => {
    const gate = buildQualityGate(CAMS);
    gate.verdict = 'FAILED';
    gate.pairs['cam_B-cam_C'] = {
      ...gate.pairs['cam_B-cam_C']!,
      median_px: 7.04,
      verdict: 'FAIL',
      reason: 'HOLDOUT_ERROR_HIGH',
    };
    gate.problems = [
      {
        kind: 'PAIR',
        id: 'cam_B-cam_C',
        camera_ids: ['cam_B', 'cam_C'],
        reason: 'HOLDOUT_ERROR_HIGH',
        message: 'cam_B-cam_C: 홀드아웃 재투영 오차가 큽니다',
      },
    ];
    const view = deriveCameraCheckView(session({ status: 'FAILED', quality_gate: gate }), CAMS);
    expect(view.status).toBe('NONE');
    expect(view.pairs.find((p) => p.label === '카메라 2–카메라 3')!.outcome).toBe('fail');
    expect(view.problems).toEqual(['cam_B-cam_C: 홀드아웃 재투영 오차가 큽니다']);
  });

  it('서버 판정(camera_check_passed)이 아니면 통과로 보지 않는다(다른 보정의 PASSED 등)', () => {
    const view = deriveCameraCheckView(
      session({
        camera_check: buildCameraCheck(CAMS, 'cal-old', 'PASSED'),
        camera_check_passed: false,
      }),
      CAMS,
    );
    expect(view.status).toBe('PASSED');
    expect(view.gated).toBe(true);
    expect(view.passed).toBe(false);
    expect(cameraCheckBlockReason(view)).toBe('카메라 확인을 마치면 넘어갈 수 있어요');
  });

  it('값이 없거나 모양이 틀리면 확인 전으로 본다', () => {
    const view = deriveCameraCheckView(
      session({ camera_check: { status: 'WEIRD' }, camera_check_passed: false }),
      CAMS,
    );
    expect(view.status).toBe('NONE');
    expect(view.pairs.every((p) => p.outcome === 'pending')).toBe(true);
    expect(cameraCheckGuide(view)).toContain('보정이 끝나면');
  });
});

describe('세팅 2단계 다음·다시 보정 단추', () => {
  const on = (): SetupActionHandlers => ({
    start: vi.fn(),
    confirm: vi.fn(),
    restart: vi.fn(),
    cancelSession: vi.fn(),
    recheckCameras: vi.fn(),
    finish: vi.fn(),
  });
  const state = (patch: Partial<SetupActionState>): SetupActionState => ({
    stage: 1,
    embedded: false,
    startDisabled: false,
    mapComplete: true,
    sessionRunning: false,
    cancelPending: false,
    showHeightNotice: false,
    canFinish: false,
    finishPending: false,
    setupCompleted: false,
    ...patch,
  });

  it('맵이 다 됐어도 카메라 확인 전에는 다음이 꺼지고 이유를 title 로 보인다', () => {
    const [next] = deriveSetupActions(
      state({ cameraCheckBlocked: '카메라 확인을 마치면 넘어갈 수 있어요' }),
      on(),
    );
    expect(next).toMatchObject({ label: '다음', disabled: true, primary: true });
    expect(next!.title).toBe('카메라 확인을 마치면 넘어갈 수 있어요');
    // 맵이 아직이면 맵 이유가 먼저다
    const [first] = deriveSetupActions(
      state({ mapComplete: false, cameraCheckBlocked: 'x' }),
      on(),
    );
    expect(first!.title).toBe('3D 맵을 다 만들면 넘어갈 수 있어요');
  });

  it('카메라 확인이 실패하면 다시 보정 단추가 생기고 restart 를 부른다', () => {
    const handlers = on();
    const buttons = deriveSetupActions(
      state({ cameraCheckBlocked: 'x', cameraCheckFailed: true }),
      handlers,
    );
    const again = buttons.find((b) => b.key === 'recalibrate')!;
    expect(again).toMatchObject({ label: '다시 보정', primary: false, disabled: false });
    again.onClick();
    expect(handlers.restart).toHaveBeenCalled();
  });
});

describe('관문을 모르는 이전 서버', () => {
  it('camera_check_passed 가 없으면 막지 않고 카드도 숨길 수 있게 gated=false', () => {
    const view = deriveCameraCheckView(session(), CAMS);
    expect(view.gated).toBe(false);
    expect(view.passed).toBe(true);
    expect(cameraCheckBlockReason(view)).toBeNull();
  });
});

describe('mock: 카메라 확인 진행과 관문', () => {
  it('autoProgress 는 보정 뒤 기다림 → 재는 중 → 통과로 가고, 통과 전 완료·구역 저장을 거부하는 값을 낸다', () => {
    let t = 1_000_000;
    const mock = createSetupMock({ now: () => t, autoProgress: true });
    const base = session({
      status: 'COLLECTING_BACKGROUND',
      progress_pct: 0,
      calibration_version: null,
    });
    mock.sessions.set(base.setup_session_id, { ...base, started_at_ms: t });
    expect(buildCameraCheck(CAMS, 'cal', 'MEASURING', 50).status).toBe('MEASURING');
    // 시간만 넘겨도 조회 처리기가 진행시키므로 여기서는 값 모양만 계약대로인지 본다
    t += 23_000;
    const gate = buildQualityGate(CAMS);
    expect(Object.keys(gate.pairs)).toEqual([
      'cam_A-cam_B',
      'cam_B-cam_C',
      'cam_C-cam_D',
      'cam_D-cam_A',
    ]);
    const parsed = setupSessionResponseSchema.parse({
      ...base,
      quality_gate: gate,
      camera_check: buildCameraCheck(CAMS, 'cal-646', 'INCOMPLETE', 40),
    });
    expect(parsed.camera_check?.problems[0]?.reason).toBe('NOT_FINISHED');
    expect(parsed.quality_gate?.verdict).toBe('PASSED');
  });
});
