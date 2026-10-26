import { requestZones, ZONES_KEY } from '@/features/zones/api';
import type { Zone } from '@/features/zones/types';
import { readZones, toViewerZones } from '@/features/zones/zones';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';

const NONE: Zone[] = [];

/** 구역을 받지 못했을 때 다시 시도하는 간격(ms). */
const RELOAD_MS = 10_000;

/**
 * 현장 구역. 로그인마다 한 번 받고, 받지 못하면 받을 때까지 다시 시도한다. 저장하면 응답으로 목록을 바꾼다.
 * raw 는 3D 뷰어 Source 에 넘길 상자 형식이다(뷰어가 다시 검사한다). 받기 전에는 없다.
 */
export function useZones() {
  const query = useQuery({
    queryKey: ZONES_KEY,
    queryFn: ({ signal }) => requestZones(api, signal),
    staleTime: Infinity,
    refetchInterval: (q) => (q.state.data ? false : RELOAD_MS),
    refetchIntervalInBackground: true,
  });
  const zones = useMemo(() => (query.data ? readZones(query.data) : NONE), [query.data]);
  const raw = useMemo(() => (query.data ? toViewerZones(zones) : undefined), [query.data, zones]);
  return { zones, raw, failed: query.isError };
}
