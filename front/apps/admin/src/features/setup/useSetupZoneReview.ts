import {
  requestSetupZonePlan,
  saveSetupZonePlan,
  setupSessionKey,
  setupZonePlanKey,
} from '@/features/setup/api';
import {
  confirmPlanItems,
  pendingReviewCount,
  removePlanItem,
  type SetupZonePlanItem,
} from '@/features/setup/setupZonePlan';
import { useSetupZonePlanQuery } from '@/features/setup/useSetupZonePlan';
import { commitErrorText } from '@/features/zones/commitErrors';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';

// 세팅 구역 목록의 확인 필요 처리(S15P31A101-629). 새 모델 + World 이어짐으로 채운 구역을 하나씩(또는 모두) 확인하거나 목록에서
// 뺀다(완료 때 보관). 수정은 구역 편집 도구가 저장하면 확인된 것으로 바뀐다(applyCommitToPlan). 목록을 통째로 저장한다(PUT, revision).

/** 다시 받아야 하는 저장 실패: 다른 곳에서 먼저 고침, 세팅 완료·작업 시작으로 닫힘. */
const REFETCH_CODES: ReadonlySet<string> = new Set([
  'SETUP_ZONE_PLAN_CONFLICT',
  'SETUP_ZONES_CLOSED',
]);

type PlanEdit = (items: readonly SetupZonePlanItem[]) => SetupZonePlanItem[] | null;

export interface SetupZoneReview {
  /** 확인이 필요한 구역 수. */
  pending: number;
  /** 화면 ID 하나를 확인한다. */
  confirm: (viewId: string) => void;
  /** 남은 확인 필요를 모두 확인한다. */
  confirmAll: () => void;
  /** 화면 ID 하나를 목록에서 뺀다(완료 때 관제 구역에서 보관된다). */
  remove: (viewId: string) => void;
  busy: boolean;
  /** 마지막 저장 실패 문구(없으면 null). */
  error: string | null;
}

/** 세션 구역 목록의 확인 필요를 고치는 훅. 저장 응답으로 목록을 바꾼다(낙관적 갱신 없음). */
export function useSetupZoneReview(sessionId: string): SetupZoneReview {
  const queryClient = useQueryClient();
  const plan = useSetupZonePlanQuery(sessionId);

  const save = useMutation({
    mutationFn: async (edit: PlanEdit) => {
      const key = setupZonePlanKey(sessionId);
      const current = await queryClient.ensureQueryData({
        queryKey: key,
        queryFn: ({ signal }) => requestSetupZonePlan(api, sessionId, signal),
      });
      const next = edit(current.items);
      return next ? saveSetupZonePlan(api, sessionId, next, current.revision) : current;
    },
    onSuccess: (saved) => {
      queryClient.setQueryData(setupZonePlanKey(sessionId), saved);
    },
    onError: (error) => {
      if (error instanceof ApiError && REFETCH_CODES.has(error.code ?? '')) {
        void queryClient.invalidateQueries({ queryKey: setupZonePlanKey(sessionId) });
        void queryClient.invalidateQueries({ queryKey: setupSessionKey(sessionId) });
      }
    },
  });

  const { mutate, isPending } = save;
  const run = useCallback(
    (edit: PlanEdit) => {
      if (!isPending) mutate(edit);
    },
    [isPending, mutate],
  );
  const confirm = useCallback(
    (viewId: string) => run((items) => confirmPlanItems(items, [viewId])),
    [run],
  );
  const confirmAll = useCallback(() => run((items) => confirmPlanItems(items)), [run]);
  const remove = useCallback(
    (viewId: string) => run((items) => removePlanItem(items, viewId)),
    [run],
  );

  return {
    pending: pendingReviewCount(plan.data),
    confirm,
    confirmAll,
    remove,
    busy: isPending,
    error: save.error ? commitErrorText(save.error) : null,
  };
}
