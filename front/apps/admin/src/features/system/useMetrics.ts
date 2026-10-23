import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { requestMetrics } from '@/features/system/api';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';

export const METRICS_KEY = ['system', 'metrics'] as const;

/** 지표를 다시 받는 간격(ms). 서버가 1~5 Hz 로 기록한다(명세 11.2 system_metric). */
const RELOAD_MS = 5_000;

/**
 * 구간별 지연 지표. 명세상 관리자만 볼 수 있어 관리자일 때만 부른다.
 * 못 받으면(권한·실패) 칸을 비운다. 0 으로 채우지 않는다.
 */
export function useMetrics() {
  const admin = useCurrentRole() === 'ADMIN';
  const query = useQuery({
    queryKey: METRICS_KEY,
    queryFn: ({ signal }) => requestMetrics(api, signal),
    enabled: admin,
    refetchInterval: RELOAD_MS,
    // 실패하면 바로 비우고 다음 갱신 때 다시 받는다.
    retry: false,
  });
  // 갱신이 실패하면 이전 값을 지금 값처럼 보이지 않는다.
  return admin && !query.isError ? query.data : undefined;
}
