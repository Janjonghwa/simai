import { bandFailure, dataAgeSec, failureOf, lastDataClock } from '@/features/layout/status';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import type { HealthSnapshot } from '@/store/realtime.store';
import { validateMessage, type ViewerHealthMessage } from '@simai/contracts';
import { describe, expect, it } from 'vitest';

// GET /api/health 와 /topic/health 는 같은 viewer_health 봉투다. 서버가 주는 값(계약 예제)으로 화면 판정이 맞는지 본다.

const examples = import.meta.glob<ViewerHealthMessage>(
  '../../../../../../../contracts/examples/viewer_health*.json',
  { eager: true, import: 'default' },
);
const example = (name: string) => {
  const raw = Object.entries(examples).find(([path]) => path.endsWith(name))?.[1];
  if (!raw) throw new Error(`계약 예제가 없다: ${name}`);
  const checked = validateMessage('viewer_health', raw);
  if (!checked.ok) throw new Error(`계약 예제가 계약을 통과하지 못한다: ${name}`);
  return checked.message;
};

const snapshot = (message: ViewerHealthMessage, receivedAt = message.sent_at): HealthSnapshot => ({
  payload: message.payload,
  receivedAt,
  clockOffsetMs: 0,
});

describe('서버 viewer_health 봉투로 하는 장애 판정', () => {
  it('정상 예제는 장애가 없고 데이터 연령을 EC2 가 잰 값에서 읽는다', () => {
    const message = example('viewer_health.json');
    const health = snapshot(message);
    expect(failureOf(health, true)).toBeUndefined();
    expect(dataAgeSec(health, message.sent_at)).toBeCloseTo(0.21);
    expect(lastDataClock(health)).toBeDefined();
  });

  it('원격 전달 단절 예제(Tier 0 끊김, 노트북 정상, 데이터 연령 null)는 REMOTE_LINK_LOST 다', () => {
    const message = example('viewer_health.remote_link_lost.json');
    const health = snapshot(message);
    expect(message.payload).toMatchObject({
      remote_state: 'REMOTE_LINK_LOST',
      tier0_link: 'DISCONNECTED',
      laptop: { link: 'CONNECTED' },
      data_age_ms: null,
    });
    expect(failureOf(health, true)).toBe('REMOTE_LINK_LOST');
    expect(bandFailure({ phase: 'OPEN', openedAt: 0, health }, true)).toBe('REMOTE_LINK_LOST');
    // 데이터 연령을 모르면(null) 숫자로 만들지 않는다.
    expect(dataAgeSec(health, message.sent_at)).toBeUndefined();
    expect(lastDataClock(health)).toBeUndefined();
  });
});

describe('remote_state × laptop.link', () => {
  const state = (
    remote_state: 'NORMAL' | 'REMOTE_LINK_LOST' | 'CONTROL_HALTED',
    link: 'CONNECTED' | 'DISCONNECTED',
  ) => {
    const base = viewerHealthAt(1_760_342_400_000);
    return snapshot({
      ...base,
      payload: { ...base.payload, remote_state, laptop: { link, last_heartbeat_at: null } },
    });
  };

  it.each([
    ['NORMAL', 'CONNECTED', undefined],
    // 노트북 하트비트가 막 끊긴 사이(EC2 가 아직 정지로 판정하기 전)는 장애로 띄우지 않는다.
    ['NORMAL', 'DISCONNECTED', undefined],
    ['REMOTE_LINK_LOST', 'CONNECTED', 'REMOTE_LINK_LOST'],
    // 원격 단절은 노트북 연결과 상관없이 EC2 가 알린 대로다.
    ['REMOTE_LINK_LOST', 'DISCONNECTED', 'REMOTE_LINK_LOST'],
    ['CONTROL_HALTED', 'CONNECTED', 'CONTROL_HALTED'],
    ['CONTROL_HALTED', 'DISCONNECTED', 'DEVICE_DOWN'],
  ] as const)('%s + 노트북 %s → %s', (remote, link, expected) => {
    expect(failureOf(state(remote, link), true)).toBe(expected);
    // EC2 와 끊겼으면 마지막 상태로 장애를 알리지 않는다.
    expect(failureOf(state(remote, link), false)).toBeUndefined();
  });
});
