import { seedSetupZonePlan, setupZonePlanKey } from '@/features/setup/api';
import type { SetupZonePlanView } from '@/features/setup/setupZonePlan';
import type { SetupSessionResponse } from '@/features/setup/types';
import { useSetupZonePlanQuery } from '@/features/setup/useSetupZonePlan';
import { api } from '@/lib/api';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';

// 구역 단계에 처음 들어올 때 세션 구역 목록 채우기(S15P31A101-615, -629). 구역 단계의 구역 편집·AI 초안 수락은 관제 구역이 아니라
// 세션 구역 목록(-612)에 저장하고 세팅 완료가 그 목록으로 관제 구역을 맞춘다. 같은 World 인 세팅은 빈 목록(revision 0)을 지금 관제
// 구역(zone_id 를 이어 쓴다)으로 한 번 채워 두어야 완료 때 기존 구역이 보관되지 않는다. 채우기는 서버가 한다(POST .../zones/seed):
// - 이전 모델 재사용(REUSE): 확인 표시 없이 채운다.
// - 새 모델 + World 이어짐(world_anchor.accepted): 현장이 조금 바뀌었을 수도 많이 바뀌었을 수도 있어, 채운 구역마다 '확인 필요'로 두고
//   사람이 확인·수정·삭제한다(SetupZoneReviewCard). 남아 있으면 세팅을 완료할 수 없다.
// - 새 모델 + World 끊김: 기존 구역 좌표가 새 3D 와 맞지 않아 채우지 않는다(빈 목록에서 새로 그린다).

/** 같은 World 라 지금 관제 구역을 이어 쓸 수 있는 세팅인지(재사용, 또는 새 모델의 World 다시 맞추기 채택). */
export function sharesLiveWorld(session: SetupSessionResponse | null | undefined): boolean {
  if (!session) return false;
  return session.mode === 'REUSE' || session.world_anchor?.accepted === true;
}

/** 새 모델 + World 이어짐: 채운 구역을 사람이 하나씩 확인하는 세팅(재사용은 확인 표시 없이 그대로 잇는다). */
export function reviewsCarriedZones(session: SetupSessionResponse | null | undefined): boolean {
  return session != null && session.mode !== 'REUSE' && session.world_anchor?.accepted === true;
}

/** 이 세션 목록을 채워야 하는 때의 목록(같은 World 이고 아직 아무것도 저장하지 않은 목록). 아니면 null. */
function emptySharedPlan(
  session: SetupSessionResponse | null,
  plan: SetupZonePlanView | undefined,
): SetupZonePlanView | null {
  if (!sharesLiveWorld(session) || !plan) return null;
  return plan.revision === 0 && plan.items.length === 0 ? plan : null;
}

/**
 * 구역 단계에서 같은 World 세션의 빈 구역 목록을 지금 관제 구역으로 채운다(세션마다 한 번). session 이 null 이면 아무것도 하지
 * 않는다(구역 단계가 아니거나 이미 완료). 채우지 못하면 목록을 다시 받고 다시 시도하지 않는다.
 */
export function useSeedSetupZonePlan(session: SetupSessionResponse | null): void {
  const queryClient = useQueryClient();
  const sessionId = session?.setup_session_id ?? null;
  const plan = useSetupZonePlanQuery(sessionId);
  const seeded = useRef<string | null>(null);

  const seed = useMutation({
    mutationFn: (id: string) => seedSetupZonePlan(api, id),
    onSuccess: (saved, id) => {
      queryClient.setQueryData(setupZonePlanKey(id), saved);
    },
    onError: (_error, id) => {
      void queryClient.invalidateQueries({ queryKey: setupZonePlanKey(id) });
    },
  });

  const emptyPlan = emptySharedPlan(session, plan.data);
  const { mutate } = seed;
  useEffect(() => {
    if (sessionId === null || emptyPlan === null || seeded.current === sessionId) return;
    seeded.current = sessionId;
    mutate(sessionId);
  }, [sessionId, emptyPlan, mutate]);
}
