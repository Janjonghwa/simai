import { useCanAck } from '@/features/events/ack';
import { SEVERITY_RANK } from '@/features/events/constants';
import { isAlarming, type EventEntry } from '@/features/events/entries';
import { alarmSound, type RingSeverity } from '@/lib/alarmSound';
import { useEffect, useSyncExternalStore } from 'react';

/**
 * 울릴 등급: 확인 대기 경보 중 가장 높은 등급 하나(CRITICAL > HIGH). 없으면 null.
 * 열람 계정은 확인 권한이 없어 소리를 끌 수 없으므로 울리지 않는다(배너·점멸로만 본다).
 * EC2 연결이 끊겨도 울린다: 마지막으로 안 확인 상태의 경보가 확인(ACK)될 때까지 끄지 않는다(관제 데모와 같다).
 */
export function ringingSeverity(
  entries: readonly EventEntry[],
  canAck: boolean,
): RingSeverity | null {
  if (!canAck) return null;
  let top: RingSeverity | null = null;
  for (const entry of entries) {
    if (!isAlarming(entry)) continue;
    const { severity } = entry.event;
    // isAlarming 이 HIGH 이상만 통과시키므로 여기서는 두 등급뿐이다.
    if (severity !== 'CRITICAL' && severity !== 'HIGH') continue;
    if (!top || SEVERITY_RANK[severity] > SEVERITY_RANK[top]) top = severity;
  }
  return top;
}

/** 확인 대기 경보가 있는 동안 가장 높은 등급의 경보음을 되풀이한다(명세 FR-8: 소리는 확인까지 반복). */
export function useAlarmSound(entries: readonly EventEntry[]) {
  const severity = ringingSeverity(entries, useCanAck());
  useEffect(() => {
    alarmSound.ring(severity);
    // 화면이 사라지면(로그아웃 등) 울림을 멈춘다. 등급이 바뀔 때는 곧바로 새 등급으로 이어진다.
    return () => alarmSound.ring(null);
  }, [severity]);
}

/** 소리 상태(차단됨·꺼짐·울릴 등급). 상단 바 소리 버튼이 쓴다. */
export function useAlarmSoundState() {
  return useSyncExternalStore(alarmSound.subscribe, alarmSound.getSnapshot);
}
