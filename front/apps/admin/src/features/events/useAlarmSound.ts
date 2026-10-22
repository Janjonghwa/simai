import { useCanAck } from '@/features/events/ack';
import { isAlarming, type EventEntry } from '@/features/events/events';
import { useLinkAlive } from '@/hooks/useLinkAlive';
import { ALARM_PERIOD_MS, alarmSound } from '@/lib/alarmSound';
import { useEffect, useSyncExternalStore } from 'react';

/**
 * 소리를 낼지: 확인 대기 경보가 있고, EC2 와 이어져 있고, 확인할 수 있는 계정일 때.
 * 끊기면 지금 상태를 모르므로 멈추고 상단 바가 연결 끊김을 보인다(현장 노트북 경보가 1차 채널, 명세 3.10).
 * 열람 계정은 확인 권한이 없어 소리를 끌 수 없으므로 울리지 않는다(배너·점멸로만 본다).
 */
export const shouldRing = (entries: readonly EventEntry[], alive: boolean, canAck: boolean) =>
  canAck && alive && entries.some(isAlarming);

/** 확인 대기 경보가 있는 동안 경보음을 되풀이한다(명세 FR-8: 소리는 확인까지 반복). */
export function useAlarmSound(entries: readonly EventEntry[]) {
  const ringing = shouldRing(entries, useLinkAlive(), useCanAck());
  useEffect(() => {
    if (!ringing) return;
    alarmSound.beep();
    const id = setInterval(() => alarmSound.beep(), ALARM_PERIOD_MS);
    return () => clearInterval(id);
  }, [ringing]);
}

/** 소리를 낼 수 있는지(브라우저가 막고 있으면 false). */
export function useAlarmSoundReady() {
  return useSyncExternalStore(alarmSound.subscribe, () => alarmSound.ready);
}
