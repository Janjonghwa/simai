import {
  bindWatch,
  releaseWatch,
  requestWatchBindings,
  WATCH_BINDINGS_KEY,
  type WatchBinding,
} from '@/features/watch/api';
import { linkKeyOf, watchBadges, watchesOfLinkKey, watchRows } from '@/features/watch/watchStatus';
import { api } from '@/lib/api';
import { selectTrackedCount, useRealtimeStore } from '@/store/realtime.store';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo } from 'react';

const NONE: WatchBinding[] = [];

/** 배정을 다시 받는 간격(ms). 움직임 대조(-340)가 2초마다 배정을 바꿀 수 있어 짧게 둔다 */
const REFRESH_MS = 3_000;

/** 지금 배정(열람 이상). 받지 못하면 빈 목록과 failed */
export function useWatchBindings() {
  const query = useQuery({
    queryKey: WATCH_BINDINGS_KEY,
    queryFn: ({ signal }) => requestWatchBindings(api, signal),
    refetchInterval: REFRESH_MS,
    // 뒤에 있는 탭은 묻지 않는다(관제 탭이 여럿이어도 보이는 탭만)
    refetchIntervalInBackground: false,
  });
  return { bindings: query.data ?? NONE, failed: query.isError };
}

/** 워치 줄·추적 인원·활성 워치 수. 활성 워치는 지금 연결된 워치(health watches.connected) */
export function useWatchSummary() {
  const { bindings, failed } = useWatchBindings();
  const watches = useRealtimeStore((s) => s.health?.payload.watches);
  const tracked = useRealtimeStore(selectTrackedCount);
  const rows = useMemo(() => watchRows(watches, bindings), [watches, bindings]);
  // 머리말 수치도 목록과 같은 줄로 센다(health 에 없는 배정 워치가 섞여도 서로 어긋나지 않게)
  return {
    rows,
    tracked,
    registered: rows.length,
    connected: rows.filter((r) => r.connected).length,
    failed,
  };
}

/**
 * 3D 이름표 글(World ID → 글). 연결은 기기:연결 문자열만 구독하고 배정은 react-query 구조 공유로 같은 참조라,
 * 배정·연결이 실제로 바뀔 때만 새 객체다(3D 호스트를 1초마다 다시 그리지 않는다)
 */
export function useWatchBadges() {
  const { bindings } = useWatchBindings();
  const links = useRealtimeStore((s) => linkKeyOf(s.health?.payload.watches));
  return useMemo(
    () => watchBadges(watchRows(watchesOfLinkKey(links), bindings)),
    [links, bindings],
  );
}

/** 관리자 잇기·풀기. 성공·실패와 관계없이 배정을 다시 받는다 */
export function useWatchBindingMutations() {
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: WATCH_BINDINGS_KEY });
  const bind = useMutation({
    mutationFn: ({ deviceId, worldId }: { deviceId: string; worldId: string }) =>
      bindWatch(api, deviceId, worldId),
    onSettled: refresh,
  });
  const release = useMutation({
    mutationFn: (deviceId: string) => releaseWatch(api, deviceId),
    onSettled: refresh,
  });
  return { bind, release };
}
