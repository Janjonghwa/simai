import { saveSetupZonePlan, setupZonePlanKey } from '@/features/setup/api';
import { liveZonesAsItems, type SetupZonePlanView } from '@/features/setup/setupZonePlan';
import type { SetupSessionResponse } from '@/features/setup/types';
import { useSetupZonePlanQuery } from '@/features/setup/useSetupZonePlan';
import { requestZones, ZONES_KEY } from '@/features/zones/api';
import { api } from '@/lib/api';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';

// 이전 모델 재사용(REUSE)의 세션 구역 목록 시드(S15P31A101-615). 구역 단계의 구역 편집·AI 초안 수락은 관제 구역이 아니라 세션 구역
// 목록(-612)에 저장하고 세팅 완료가 그 목록으로 관제 구역을 맞춘다. 재사용은 같은 World 라 처음 들어올 때 빈 목록(revision 0)을
// 지금 관제 구역(zone_id 를 이어 쓴다)으로 한 번 채워 두어야 완료 때 기존 구역이 보관되지 않는다. 새 모델은 빈 목록에서 시작한다.

/** 이 세션 목록을 채워야 하는 때의 목록(이전 모델 재사용이고 아직 아무것도 저장하지 않은 목록). 아니면 null. */
function emptyReusePlan(
  mode: SetupSessionResponse['mode'] | undefined,
  plan: SetupZonePlanView | undefined,
): SetupZonePlanView | null {
  if (mode !== 'REUSE' || !plan) return null;
  return plan.revision === 0 && plan.items.length === 0 ? plan : null;
}

/**
 * 구역 단계에서 이전 모델 재사용 세션의 빈 구역 목록을 지금 관제 구역으로 채운다(세션마다 한 번). session 이 null 이면 아무것도 하지
 * 않는다(구역 단계가 아니거나 이미 완료). 목록을 받지 못했거나 채우지 못하면 조용히 넘어가고 다시 시도하지 않는다.
 */
export function useSeedSetupZonePlan(session: SetupSessionResponse | null): void {
  const queryClient = useQueryClient();
  const sessionId = session?.setup_session_id ?? null;
  const plan = useSetupZonePlanQuery(sessionId);
  const seeded = useRef<string | null>(null);

  const seed = useMutation({
    mutationFn: async ({ id, current }: { id: string; current: SetupZonePlanView }) => {
      const live = await queryClient.fetchQuery({
        queryKey: ZONES_KEY,
        queryFn: ({ signal }) => requestZones(api, signal),
        staleTime: 0,
      });
      return saveSetupZonePlan(
        api,
        id,
        [...current.items, ...liveZonesAsItems(live, current.items)],
        current.revision,
      );
    },
    onSuccess: (saved, { id }) => {
      queryClient.setQueryData(setupZonePlanKey(id), saved);
    },
    // 그 사이 다른 곳에서 목록을 먼저 고쳤으면(revision 충돌) 최신 목록을 다시 받는다
    onError: (_error, { id }) => {
      void queryClient.invalidateQueries({ queryKey: setupZonePlanKey(id) });
    },
  });

  const emptyPlan = emptyReusePlan(session?.mode, plan.data);
  const { mutate } = seed;
  useEffect(() => {
    if (sessionId === null || emptyPlan === null || seeded.current === sessionId) return;
    seeded.current = sessionId;
    mutate({ id: sessionId, current: emptyPlan });
  }, [sessionId, emptyPlan, mutate]);
}
