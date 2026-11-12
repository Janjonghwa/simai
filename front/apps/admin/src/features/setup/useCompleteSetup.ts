import {
  completeSetupSession,
  requestSetupZonePlan,
  setupSessionKey,
  setupZonePlanKey,
} from '@/features/setup/api';
import { EMPTY_ZONE_PLAN_CONFIRM } from '@/features/setup/SetupCompleteCard';
import { setupCompleteErrorMessage } from '@/features/setup/setupErrorMessage';
import type { AppliedZones, SetupSessionResponse } from '@/features/setup/types';
import { TBM_WORKS_KEY } from '@/features/tbm/api';
import { ZONES_KEY } from '@/features/zones/api';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { WORK_KEY } from '@/lib/queryKeys';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useCallback, useState } from 'react';

// 세팅 구역 단계의 「세팅 완료」(C17, S15P31A101-615). 서버가 한 번에 구역 목록을 관제 구역으로 적용하고, 완료를 기록하고,
// (새 모델이면) 이 세션 맵을 관제 맵으로 켜고, 구성 묶음을 Tier 0 에 보낸다. 완료 뒤에는 이 세팅으로 구역·보강을 고치지 않는다(하루 1회).

export interface CompleteSetup {
  /** 세팅을 완료한다. 완료했으면 true, 취소했거나 거부·실패했으면 false(이유는 error). */
  run: () => Promise<boolean>;
  /** 구역 목록을 확인하거나 완료를 요청하는 중. */
  pending: boolean;
  /** 마지막 시도의 실패 안내(없으면 null). 다시 시도하면 지운다. */
  error: string | null;
  /** 완료 응답의 구역 적용 결과(완료하기 전이거나 다른 화면에서 이미 완료했으면 null). */
  applied: AppliedZones | null;
}

/** 세팅 완료를 부르는 훅. 저장한 구역 목록이 비었으면 관제 구역이 모두 보관되므로 먼저 확인창으로 묻는다. */
export function useCompleteSetup(session: SetupSessionResponse | null): CompleteSetup {
  const queryClient = useQueryClient();
  const id = session?.setup_session_id ?? null;
  const [applied, setApplied] = useState<AppliedZones | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const complete = useMutation({
    mutationFn: (sessionId: string) => completeSetupSession(api, sessionId),
    onSuccess: (response, sessionId) => {
      const { applied_zones: zones, ...view } = response;
      setApplied(zones);
      queryClient.setQueryData(setupSessionKey(sessionId), view);
      void queryClient.invalidateQueries({ queryKey: setupSessionKey(sessionId) });
      // 작업 관리의 진행(SETUP_DONE)·보고서 초안 상태를 다시 받는다
      void queryClient.invalidateQueries({ queryKey: WORK_KEY });
      // 관제가 보는 작업 목록(대기 화면 판정)도 15초 주기를 기다리지 않고 바로 다시 받는다
      void queryClient.invalidateQueries({ queryKey: TBM_WORKS_KEY });
    },
    onError: (e, sessionId) => {
      setError(setupCompleteErrorMessage(e));
      // 목록의 기존 구역이 그 사이 바뀌었거나 이미 완료됐으면 목록·관제 구역·세션을 다시 받는다
      if (
        e instanceof ApiError &&
        (e.code === 'SETUP_ZONE_STALE' || e.code === 'SETUP_ALREADY_COMPLETED')
      ) {
        void queryClient.invalidateQueries({ queryKey: setupZonePlanKey(sessionId) });
        void queryClient.invalidateQueries({ queryKey: ZONES_KEY });
        void queryClient.invalidateQueries({ queryKey: setupSessionKey(sessionId) });
      }
    },
  });

  const pending = checking || complete.isPending;
  const { mutateAsync } = complete;

  const run = useCallback(async (): Promise<boolean> => {
    if (id === null || pending) return false;
    setError(null);
    setChecking(true);
    try {
      const plan = await requestSetupZonePlan(api, id);
      if (plan.items.length === 0 && !window.confirm(EMPTY_ZONE_PLAN_CONFIRM)) return false;
    } catch (e) {
      setError(setupCompleteErrorMessage(e));
      return false;
    } finally {
      setChecking(false);
    }
    try {
      await mutateAsync(id);
      return true;
    } catch {
      // 실패 안내와 목록 다시 받기는 변이의 onError 가 이미 했다
      return false;
    }
  }, [id, pending, mutateAsync]);

  return { run, pending, error, applied };
}
