import {
  calibrationLabel,
  cameraLabel,
  chipState,
  clockLabel,
  configLabel,
  dataAgeSec,
  sceneLabel,
} from '@/features/layout/status';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import type { HealthSnapshot } from '@/store/realtime.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { describe, expect, it } from 'vitest';

const T = 1_760_342_400_000;

function snapshot(change: Partial<ViewerHealthPayload> = {}, receivedAt = T): HealthSnapshot {
  const payload = { ...viewerHealthAt(T).payload, ...change };
  return { payload, receivedAt, clockOffsetMs: 0 };
}

describe('chipState', () => {
  const open = { phase: 'OPEN' as const, openedAt: T, health: snapshot() };

  it('연결 전에는 비우고, 이어져 있으면 System Health, 끊기면 LOST 다', () => {
    expect(chipState({ phase: 'IDLE', openedAt: null, health: null }, false)).toBeUndefined();
    expect(chipState({ phase: 'CONNECTING', openedAt: null, health: null }, false)).toBeUndefined();
    expect(chipState(open, true)).toBe('NOMINAL');
    expect(chipState(open, false)).toBe('LOST');
    // 다시 연결하는 중에도 지금 상태를 모르므로 LOST 다.
    expect(chipState({ ...open, phase: 'CONNECTING' }, false)).toBe('LOST');
  });

  it('열렸지만 health 를 아직 못 받았으면 비운다', () => {
    expect(chipState({ ...open, health: null }, true)).toBeUndefined();
  });
});

describe('상단 바 문구', () => {
  it('정상 카메라 수, 보정 상태·버전, 구성, 장면 버전을 만든다', () => {
    const health = snapshot();
    expect(cameraLabel(health)).toBe('4/4');
    expect(calibrationLabel(health)).toBe('CALIBRATED cal-7');
    expect(configLabel(health)).toBe('cfg-12');
    expect(sceneLabel(health)).toBe('scene-3');
  });

  it('OK 가 아닌 카메라는 정상 수에서 뺀다', () => {
    const health = snapshot({
      tier0_health: {
        cameras: [
          { id: 'cam1', status: 'OK' },
          { id: 'cam2', status: 'NO_SIGNAL' },
          { id: 'cam3', status: 'WAITING_KEYFRAME' },
        ],
        calibration_version: 'cal-8',
      },
    });
    expect(cameraLabel(health)).toBe('1/3');
    expect(calibrationLabel(health)).toBe('CALIBRATED cal-8');
  });

  it('Tier 0 health 가 없거나 모양이 다르면 그 칸만 비운다', () => {
    expect(cameraLabel(snapshot({ tier0_health: null }))).toBeUndefined();
    expect(cameraLabel(snapshot({ tier0_health: { cameras: 'x' } }))).toBeUndefined();
    expect(calibrationLabel(snapshot({ tier0_health: null }))).toBe('CALIBRATED');
    expect(cameraLabel(null)).toBeUndefined();
  });

  it('구성이 아직 적용되지 않았으면 발행 버전과 상태를 보인다', () => {
    const config = (sync: ViewerHealthPayload['config']['sync']) =>
      snapshot({ config: { published_version: 'cfg-13', applied_version: 'cfg-12', sync } });
    expect(configLabel(config('PENDING'))).toBe('cfg-13 적용 대기');
    expect(configLabel(config('UNVERIFIED'))).toBe('cfg-13 적용 미확인');
    expect(configLabel(config('STALE'))).toBe('cfg-13 적용 지연');
    expect(configLabel(config('CURRENT'))).toBe('cfg-12');
  });

  it('데이터 연령은 EC2 가 잰 값에 받은 뒤 흐른 시간을 더한다', () => {
    const health = snapshot({ data_age_ms: 200 }, T);
    expect(dataAgeSec(health, T)).toBeCloseTo(0.2);
    expect(dataAgeSec(health, T + 5000)).toBeCloseTo(5.2);
    expect(dataAgeSec(snapshot({ data_age_ms: null }), T)).toBeUndefined();
    expect(dataAgeSec(null, T)).toBeUndefined();
  });

  it('시계는 EC2 와의 차이를 더한 시각이다', () => {
    const now = new Date(2026, 9, 21, 14, 30, 5).getTime();
    expect(clockLabel(0, now)).toBe('14:30:05');
    expect(clockLabel(-5000, now)).toBe('14:30:00');
    expect(clockLabel(undefined, now)).toBeUndefined();
  });
});
