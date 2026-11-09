import { EXPIRED_REASON, itemStates, LINK_LOST_REASON, personPpe } from '@/features/ppe/ppeView';
import { useRealtimeStore, type PpeSnapshot } from '@/store/realtime.store';
import type { PpeViewPayload } from '@simai/contracts';
import { beforeEach, describe, expect, it } from 'vitest';

// 관제 화면 PPE 표시 규칙(명세 13.3, S15P31A101-531): 같은 World ID·identity_revision 에만, 만료·EC2 끊김이면 UNKNOWN.

const T = 1_760_342_400_000;

function payload(overrides: Partial<PpeViewPayload> = {}): PpeViewPayload {
  return {
    tier0_source_id: 'tier0-01',
    snapshot_at_ms: T,
    people: [
      {
        world_id: 'w-1',
        identity_revision: 2,
        capture_time_ms: T - 100,
        received_at_ms: T - 50,
        helmet: {
          state: 'WORN',
          reported_state: 'WORN',
          reason: 'WINDOW_WORN',
          last_evidence_at_ms: T - 100,
          expires_at_ms: T + 9_900,
        },
        vest: {
          state: 'UNKNOWN',
          reported_state: 'UNKNOWN',
          reason: 'NO_OBSERVATION',
          last_evidence_at_ms: null,
          expires_at_ms: null,
        },
      },
    ],
    ...overrides,
  };
}

const RECEIVED = 5_000;
const snapshots = (p = payload()): Record<string, PpeSnapshot> => ({
  [p.tier0_source_id]: { payload: p, receivedAt: RECEIVED },
});

describe('personPpe', () => {
  it('같은 World ID·차수면 보낸 상태와 근거 시각을 보인다', () => {
    const ppe = personPpe(snapshots(), 'w-1', 2, RECEIVED + 1_000, true);
    expect(ppe?.helmet).toEqual({
      state: 'WORN',
      reason: 'WINDOW_WORN',
      lastEvidenceAtMs: T - 100,
    });
    expect(ppe?.vest.state).toBe('UNKNOWN');
  });

  it('identity_revision 이 다르거나 없는 사람이면 붙이지 않는다(이전 ID 상태를 넘기지 않음)', () => {
    expect(personPpe(snapshots(), 'w-1', 3, RECEIVED, true)).toBeNull();
    expect(personPpe(snapshots(), 'w-9', 0, RECEIVED, true)).toBeNull();
  });

  it('다음 스냅샷 전이라도 expires_at_ms 가 지나면 UNKNOWN(EVIDENCE_EXPIRED)이다', () => {
    // 스냅샷 시각 T 에서 10초 지나 만료(T + 9_900)를 넘겼다
    const ppe = personPpe(snapshots(), 'w-1', 2, RECEIVED + 10_000, true);
    expect(ppe?.helmet).toEqual({
      state: 'UNKNOWN',
      reason: EXPIRED_REASON,
      lastEvidenceAtMs: T - 100,
    });
  });

  it('EC2 연결이 끊기면 UNKNOWN 이다', () => {
    const ppe = personPpe(snapshots(), 'w-1', 2, RECEIVED, false);
    expect(ppe?.helmet.state).toBe('UNKNOWN');
    expect(ppe?.helmet.reason).toBe(LINK_LOST_REASON);
  });

  it('EC2 가 이미 만료로 바꾼 값은 그대로 UNKNOWN 이다(보낸 값으로 되돌리지 않음)', () => {
    const p = payload();
    p.people[0]!.helmet = { ...p.people[0]!.helmet, state: 'UNKNOWN', reason: EXPIRED_REASON };
    expect(personPpe(snapshots(p), 'w-1', 2, RECEIVED, true)?.helmet.state).toBe('UNKNOWN');
  });
});

describe('itemStates', () => {
  const tracks = [
    { worldId: 'w-1', identityRevision: 2 },
    { worldId: 'w-2', identityRevision: 0 },
  ];

  it('3D 에는 붙일 상태가 있는 사람만 담고, 안전모와 조끼를 따로 준다', () => {
    expect(itemStates(snapshots(), tracks, RECEIVED, true, 'helmet')).toEqual({ 'w-1': 'WORN' });
    expect(itemStates(snapshots(), tracks, RECEIVED, true, 'vest')).toEqual({ 'w-1': 'UNKNOWN' });
  });

  it('EC2 연결이 끊기면 둘 다 UNKNOWN 이고, 차수가 다른 사람은 담지 않는다', () => {
    expect(itemStates(snapshots(), tracks, RECEIVED, false, 'helmet')).toEqual({
      'w-1': 'UNKNOWN',
    });
    const stale = [{ worldId: 'w-1', identityRevision: 9 }];
    expect(itemStates(snapshots(), stale, RECEIVED, true, 'vest')).toEqual({});
  });
});

describe('receivePpe', () => {
  beforeEach(() => useRealtimeStore.setState(useRealtimeStore.getInitialState(), true));

  it('더 새 스냅샷만 받고 Tier 0 마다 따로 둔다', () => {
    const store = useRealtimeStore.getState();
    store.receivePpe({ payload: payload({ snapshot_at_ms: T + 1_000 }), receivedAt: 1 });
    store.receivePpe({ payload: payload({ snapshot_at_ms: T, people: [] }), receivedAt: 2 });
    store.receivePpe({ payload: payload({ tier0_source_id: 'tier0-02' }), receivedAt: 3 });
    const { ppe } = useRealtimeStore.getState();
    expect(ppe['tier0-01']?.payload.people).toHaveLength(1);
    expect(ppe['tier0-02']?.receivedAt).toBe(3);
  });

  it('새 세션(begin)이면 비운다', () => {
    useRealtimeStore.getState().receivePpe({ payload: payload(), receivedAt: 1 });
    useRealtimeStore.getState().begin();
    expect(useRealtimeStore.getState().ppe).toEqual({});
  });
});
