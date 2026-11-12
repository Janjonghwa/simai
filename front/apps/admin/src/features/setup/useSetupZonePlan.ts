import { requestSetupZonePlan, setupZonePlanKey } from '@/features/setup/api';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';

/**
 * 세팅 중 구역 목록(-612). sessionId 가 없으면 받지 않는다. 저장하면 응답으로 바꾼다(useZoneCommitMutation).
 * 구역 단계가 새로 열릴 때마다 다시 받아 다른 PC 에서 고친 목록(revision)을 따라간다.
 */
export function useSetupZonePlanQuery(sessionId: string | null) {
  return useQuery({
    queryKey: setupZonePlanKey(sessionId ?? ''),
    queryFn: ({ signal }) => requestSetupZonePlan(api, sessionId!, signal),
    enabled: sessionId !== null,
    staleTime: 0,
    refetchOnWindowFocus: false,
    retry: false,
  });
}
