import { useLiveEvents } from '@/features/events/useLiveEvents';

/** 경보를 화면에서 임의로 숨기지 않고 전달한다. */
export function useVisibleLiveEvents() {
  return useLiveEvents();
}
