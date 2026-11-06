import { BUNDLES_KEY, requestBundles } from '@/features/zones/api';
import { configBundleSchema, type ConfigBundle } from '@/features/zones/types';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';

/** 적용 대기 동안 적용 상태를 다시 받는 간격(ms). 서버는 Tier 0 의 config_ack 를 받으면 상태를 바꾼다. */
const PENDING_POLL_MS = 3_000;

/** 받은 묶음 목록(새것부터)에서 형식이 맞는 것만 고른다. */
export function readBundles(items: readonly unknown[]): ConfigBundle[] {
  return items.flatMap((item) => {
    const parsed = configBundleSchema.safeParse(item);
    return parsed.success ? [parsed.data] : [];
  });
}

/**
 * 최근 구성 묶음(GET /api/config-bundles, 새것부터 20개). 마지막 묶음이 적용 대기면 다시 받는다. 구역 발행 상태·
 * PPE 요구 미수신·R9 해소 구분이 같은 캐시를 쓴다. enabled 가 false 면 받지 않고 캐시에 있는 것만 돌려준다.
 */
export function useRecentBundles(enabled = true) {
  return useQuery({
    queryKey: BUNDLES_KEY,
    queryFn: ({ signal }) => requestBundles(api, signal),
    select: readBundles,
    enabled,
    refetchInterval: (q) =>
      readBundles(q.state.data ?? [])[0]?.status === 'PENDING' ? PENDING_POLL_MS : false,
  });
}
