import { cameraState, framesArriving, showsState, stateText } from '@/features/cctv/cctv';
import { healthCameras } from '@/features/cctv/cctvCameras';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import type { HealthSnapshot } from '@/store/realtime.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { describe, expect, it } from 'vitest';

// T 는 mock 바퀴(40초)의 0초라 카메라 4대가 정상이다. T + 20초부터 6초 동안 cam_C 가 키프레임을 기다린다.
const T = 1_760_342_400_000;

function snapshot(t = T, change: Partial<ViewerHealthPayload> = {}): HealthSnapshot {
  return { payload: { ...viewerHealthAt(t).payload, ...change }, receivedAt: t, clockOffsetMs: 0 };
}

describe('healthCameras', () => {
  it('tier0_health.cameras 의 id·status·last_capture 를 읽는다', () => {
    const cameras = healthCameras(snapshot());
    expect(cameras.map((camera) => camera.id)).toEqual(['cam_A', 'cam_B', 'cam_C', 'cam_D']);
    expect(cameras[0]).toEqual({ id: 'cam_A', status: 'OK', lastCaptureMs: T - 120 });
  });

  it('키프레임을 기다리는 카메라는 status 만 있고 촬영 시각이 없다', () => {
    const cam_C = healthCameras(snapshot(T + 20_000)).find((camera) => camera.id === 'cam_C');
    expect(cam_C).toEqual({ id: 'cam_C', status: 'WAITING_KEYFRAME', lastCaptureMs: undefined });
  });

  it('health 가 없거나 Tier 0 와 끊겼으면 비운다', () => {
    expect(healthCameras(null)).toEqual([]);
    expect(healthCameras(snapshot(T, { tier0_link: 'DISCONNECTED' }))).toEqual([]);
  });

  it('형식이 틀린 칸은 그 칸만 비우고 id 가 없는 카메라는 뺀다', () => {
    const health = snapshot(T, {
      tier0_health: {
        cameras: [
          { id: 'cam_A', status: 7, last_capture: 'x' },
          { status: 'OK' },
          { id: 'cam_B', status: 'NO_SIGNAL', last_capture: null },
        ],
      },
    });
    expect(healthCameras(health)).toEqual([
      { id: 'cam_A', status: undefined, lastCaptureMs: undefined },
      { id: 'cam_B', status: 'NO_SIGNAL', lastCaptureMs: undefined },
    ]);
    expect(healthCameras(snapshot(T, { tier0_health: { cameras: 'x' } }))).toEqual([]);
  });
});

describe('cameraState', () => {
  const camera = (status?: string) => ({ id: 'cam_A', status });

  it('status OK 이고 World 가 들어오면 실시간이다', () => {
    expect(cameraState(camera('OK'), true)).toBe('live');
  });

  it('시각 동기만 무효인 카메라(SYNC_INVALID)는 프레임이 들어오므로 실시간이다', () => {
    expect(cameraState(camera('SYNC_INVALID'), true)).toBe('live');
  });

  it.each(['NO_SIGNAL', 'WAITING_KEYFRAME'])(
    '%s 는 끊김이다(World 가 멈춰도 끊김이 먼저)',
    (status) => {
      expect(cameraState(camera(status), true)).toBe('down');
      expect(cameraState(camera(status), false)).toBe('down');
    },
  );

  it('World 가 멈췄거나 카메라·상태를 알 수 없으면 정지다', () => {
    expect(cameraState(camera('OK'), false)).toBe('frozen');
    expect(cameraState(undefined, true)).toBe('frozen');
    expect(cameraState(camera(undefined), true)).toBe('frozen');
  });

  it('칸 글은 실시간일 때만 카메라 지연(ms)을 붙인다', () => {
    expect(stateText('live', 180)).toBe('실시간 · 180 ms');
    expect(stateText('live')).toBe('실시간');
    expect(stateText('down', 180)).toBe('끊김');
  });

  it('정지는 배지를 두지 않고 끊김·실시간은 둔다', () => {
    expect(showsState('frozen')).toBe(false);
    expect(showsState('down')).toBe(true);
    expect(showsState('live')).toBe(true);
  });
});

describe('framesArriving', () => {
  it('중계가 열려 있고 그리는 중이며 마지막 프레임이 지연되지 않았을 때만 참이다', () => {
    expect(framesArriving('playing', 'open', 0.2)).toBe(true);
    expect(framesArriving('playing', 'open', 1.5)).toBe(false);
    expect(framesArriving('waiting_keyframe', 'open', undefined)).toBe(false);
    expect(framesArriving('decode_error', 'open', 0.1)).toBe(false);
    expect(framesArriving('playing', 'unavailable', 0.1)).toBe(false);
    expect(framesArriving(undefined, 'open', undefined)).toBe(false);
  });
});
