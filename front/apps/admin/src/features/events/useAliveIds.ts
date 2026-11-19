import type { AliveIds } from '@/features/events/incidents';
import { ALIVE_ID_SEPARATOR, selectAliveIdsKey, useRealtimeStore } from '@/store/realtime.store';
import { useMemo } from 'react';

/**
 * 지금 world_state 트랙에 살아 있는(LOST 제외, 병합 이전 aliases 포함) 사람의 World ID 집합.
 * world_state 를 아직 못 받았으면 null 이다. 원시 문자열 셀렉터를 써서 사람 목록이 바뀔 때만 다시 그린다.
 */
export function useAliveIds(): AliveIds {
  const aliveKey = useRealtimeStore(selectAliveIdsKey);
  return useMemo(
    () => (aliveKey === null ? null : new Set(aliveKey ? aliveKey.split(ALIVE_ID_SEPARATOR) : [])),
    [aliveKey],
  );
}
