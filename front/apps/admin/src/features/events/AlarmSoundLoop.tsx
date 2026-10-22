import { useAlarmSound } from '@/features/events/useAlarmSound';
import { useLiveEvents } from '@/features/events/useLiveEvents';

/**
 * 관제 앱 어느 화면에 있든 확인 대기 경보가 있으면 소리를 낸다. 그리는 것은 없다.
 * 레이아웃이 이벤트마다 다시 그려지지 않게 따로 둔다.
 */
export function AlarmSoundLoop() {
  const { entries } = useLiveEvents();
  useAlarmSound(entries);
  return null;
}
