import { upsertEvents, type EventEntry } from '@/features/events/events';
import type { EventItem } from '@/features/events/types';
import type { Realtime } from '@/lib/stomp';
import type { QueryClient } from '@tanstack/react-query';

/** 관제 화면 LIVE 이벤트 목록의 Query 키. 데이터는 LiveEvents. */
export const LIVE_EVENTS_KEY = ['events', 'live'] as const;

export interface LiveEvents {
  entries: EventEntry[];
  /**
   * REST 목록(GET /api/events)을 받아 합쳤는지. STOMP 로 먼저 받은 것만 있거나 재연결한 뒤에는 false 다.
   * 데이터가 있다고 목록을 받은 것은 아니라서 따로 둔다. false 면 useLiveEvents 가 받을 때까지 다시 시도한다.
   */
  loaded: boolean;
}

/** 이벤트들을 캐시에 반영한다. 아직 목록을 받기 전이어도 받은 것부터 쌓는다(목록이 오면 순서대로 합친다). */
export function applyEvents(queryClient: QueryClient, items: readonly EventItem[]) {
  queryClient.setQueryData<LiveEvents>(LIVE_EVENTS_KEY, (old) => {
    const entries = upsertEvents(old?.entries ?? [], items);
    return old && entries === old.entries ? old : { entries, loaded: old?.loaded ?? false };
  });
}

/**
 * 실시간 연결을 이벤트 캐시에 잇는다(CONVENTION: 화면이 따로 쓰는 메시지는 Query 캐시를 갱신한다).
 * - /topic/events → 캐시에 합친다(event_sequence 순서).
 * - 연결이 열릴 때마다 목록을 다시 받는다. 첫 목록을 받은 뒤 구독하기 전, 끊긴 동안에 놓친 전이를 채운다.
 * 해제 함수를 돌려준다.
 */
export function bindEventsCache(realtime: Pick<Realtime, 'on'>, queryClient: QueryClient) {
  const offEvent = realtime.on('riskEvent', (event) => applyEvents(queryClient, [event]));
  const offOpen = realtime.on('open', () => {
    // 다시 받을 때까지는 끊긴 동안 놓친 것이 있을 수 있다. 실패하면 받을 때까지 다시 시도한다.
    queryClient.setQueryData<LiveEvents>(
      LIVE_EVENTS_KEY,
      (old) => old && { ...old, loaded: false },
    );
    void queryClient.invalidateQueries({ queryKey: LIVE_EVENTS_KEY });
  });
  return () => {
    offEvent();
    offOpen();
  };
}
