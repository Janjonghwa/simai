import { requestZones } from '@/features/zones/api';
import type { ZoneBox } from '@/features/zones/types';
import { readZones } from '@/features/zones/zones';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';

export const ZONES_KEY = ['zones'] as const;

const NONE: ZoneBox[] = [];

/** 구역을 받지 못했을 때 다시 시도하는 간격(ms). */
const RELOAD_MS = 10_000;

/**
 * 현장 구역. 구역 편집(-189)이 생기기 전까지는 로그인마다 한 번 받고, 받지 못하면 받을 때까지 다시 시도한다.
 * raw 는 받은 응답 그대로다. 3D 뷰어 Source 에 넘기면 뷰어가 직접 검사한다.
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
  return { zones, raw: query.data, failed: query.isError };
}
