import { requestWatchBindings, WATCH_BINDINGS_KEY, type WatchBinding } from '@/features/watch/api';
import { useWatchBindEffectStore } from '@/features/watch/watchBindEffect.store';
import {
  bindKeyOf,
  linkKeyOf,
  newBinds,
  watchBadges,
  watchesOfLinkKey,
  watchRows,
} from '@/features/watch/watchStatus';
import { api } from '@/lib/api';
import { useRealtimeStore } from '@/store/realtime.store';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef } from 'react';

const NONE: WatchBinding[] = [];

/** 배정을 다시 받는 간격(ms). 움직임 대조(-340)가 2초마다 배정을 바꿀 수 있어 짧게 둔다 */
const REFRESH_MS = 3_000;

/** 지금 배정(열람 이상). 받지 못하면 빈 목록. loaded 는 한 번이라도 받았는지 */
function useWatchBindings(): { bindings: WatchBinding[]; loaded: boolean } {
  const query = useQuery({
    queryKey: WATCH_BINDINGS_KEY,
    queryFn: ({ signal }) => requestWatchBindings(api, signal),
    refetchInterval: REFRESH_MS,
    // 뒤에 있는 탭은 묻지 않는다(관제 탭이 여럿이어도 보이는 탭만)
    refetchIntervalInBackground: false,
  });
  return { bindings: query.data ?? NONE, loaded: query.isSuccess };
}

/**
 * 새로 이어진 (워치, World ID) 를 감지해 3D 강조를 띄운다(관제 화면 한 곳에서만 부른다, S15P31A101-509).
 * 처음 불러온 배정·재로드(다시 마운트)·같은 배정 유지는 효과가 없고, 이전 조회에 없던 배정만 센다.
 */
export function useWatchBindDetector() {
  const { bindings, loaded } = useWatchBindings();
  const prev = useRef<Set<string> | null>(null);
  useEffect(() => {
    if (!loaded) return;
    for (const bind of newBinds(prev.current, bindings)) {
      useWatchBindEffectStore.getState().announce(bind);
    }
    prev.current = new Set(bindings.map(bindKeyOf));
  }, [bindings, loaded]);
  useEffect(() => () => useWatchBindEffectStore.getState().clear(), []);
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
