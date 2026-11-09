import type { PpeSnapshot } from '@/store/realtime.store';

/** 명세 6장 PPE 상태 */
export type PpeState = 'WORN' | 'NOT_WORN' | 'UNKNOWN';

/** 화면에 보일 장비 하나의 상태. */
export interface PpeItemDisplay {
  state: PpeState;
  /** 보낸 사유 코드, 또는 화면이 UNKNOWN 으로 바꾼 이유(EVIDENCE_EXPIRED·EC2_DISCONNECTED). 모르는 코드는 표시만 한다. */
  reason: string;
  /** 마지막 근거 촬영 시각(ms, Tier 0 시계). 근거가 없으면 null. */
  lastEvidenceAtMs: number | null;
}

export interface PersonPpe {
  helmet: PpeItemDisplay;
  vest: PpeItemDisplay;
}

/** 관제 화면이 EC2 연결을 잃어 UNKNOWN 으로 바꿨다는 화면 전용 사유(계약 값이 아니다). */
export const LINK_LOST_REASON = 'EC2_DISCONNECTED';
export const EXPIRED_REASON = 'EVIDENCE_EXPIRED';

interface ItemView {
  state: PpeState;
  reason: string;
  last_evidence_at_ms: number | null;
  expires_at_ms: number | null;
}

/**
 * 한 사람의 화면용 PPE 상태(명세 13.3). 그 사람이 지금 그리는 트랙과 같은 identity_revision 의 상태만 붙인다.
 * - 같은 World ID 가 없거나 identity_revision 이 다르면 null(이전 ID 의 상태를 새 사람에게 넘기지 않는다).
 * - EC2 연결이 끊겼으면 UNKNOWN(EC2_DISCONNECTED).
 * - 다음 스냅샷 전이라도 착용·미착용 근거의 expires_at_ms 가 지나면 UNKNOWN(EVIDENCE_EXPIRED). 지금 EC2 시각은
 *   스냅샷 시각 + 받은 뒤 흐른 시간으로 어림한다(Tier 0·EC2 시계는 같은 UTC 로 본다, 설계 문서 잔여 위험).
 */
export function personPpe(
  snapshots: Readonly<Record<string, PpeSnapshot>>,
  worldId: string,
  identityRevision: number,
  now: number,
  linkAlive: boolean,
): PersonPpe | null {
  for (const snapshot of Object.values(snapshots)) {
    const person = snapshot.payload.people.find((p) => p.world_id === worldId);
    if (!person) continue;
    if (person.identity_revision !== identityRevision) return null;
    const serverNow = snapshot.payload.snapshot_at_ms + Math.max(0, now - snapshot.receivedAt);
    return {
      helmet: itemDisplay(person.helmet, serverNow, linkAlive),
      vest: itemDisplay(person.vest, serverNow, linkAlive),
    };
  }
  return null;
}

function itemDisplay(item: ItemView, serverNow: number, linkAlive: boolean): PpeItemDisplay {
  const lastEvidenceAtMs = item.last_evidence_at_ms;
  if (!linkAlive) return { state: 'UNKNOWN', reason: LINK_LOST_REASON, lastEvidenceAtMs };
  const decided = item.state === 'WORN' || item.state === 'NOT_WORN';
  if (decided && (item.expires_at_ms === null || item.expires_at_ms < serverNow)) {
    return { state: 'UNKNOWN', reason: EXPIRED_REASON, lastEvidenceAtMs };
  }
  return { state: item.state, reason: item.reason, lastEvidenceAtMs };
}

/** 사람 몸에 붙는 장비 종류: helmet · vest. */
export type PpeItemKind = keyof PersonPpe;

/** 3D 에 넘길 사람별 장비(안전모 또는 조끼) 상태. 붙일 상태가 있는 사람만 담는다. */
export function itemStates(
  snapshots: Readonly<Record<string, PpeSnapshot>>,
  tracks: readonly { worldId: string; identityRevision: number }[],
  now: number,
  linkAlive: boolean,
  item: PpeItemKind,
): Record<string, PpeState> {
  const states: Record<string, PpeState> = {};
  for (const track of tracks) {
    const ppe = personPpe(snapshots, track.worldId, track.identityRevision, now, linkAlive);
    if (ppe) states[track.worldId] = ppe[item].state;
  }
  return states;
}
