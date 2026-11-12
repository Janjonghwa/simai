import {
  completeSetupSession,
  requestSetupZonePlan,
  setupSessionKey,
  setupZonePlanKey,
} from '@/features/setup/api';
import { setupCompleteErrorMessage } from '@/features/setup/setupErrorMessage';
import type { AppliedZones, SetupSessionResponse } from '@/features/setup/types';
import { ZONES_KEY } from '@/features/zones/api';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { WORK_KEY } from '@/lib/queryKeys';
import { Alert } from '@simai/ui/components/alert';
import { Button } from '@simai/ui/components/button';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

// 세팅 구역 단계의 「세팅 완료」(C17, S15P31A101-615). 누르면 서버가 한 번에 구역 목록을 관제 구역으로 적용하고, 완료를 기록하고,
// (새 모델이면) 이 세션 맵을 관제 맵으로 켜고, 구성 묶음을 Tier 0 에 보낸다. 완료 뒤에는 이 세팅으로 구역·보강을 고치지 않는다(하루 1회).

/** 세팅 구역 목록이 비었을 때 완료 전에 묻는 문구(목록에 없는 관제 구역은 보관된다). */
export const EMPTY_ZONE_PLAN_CONFIRM =
  '저장한 구역이 없어 지금 관제 구역이 모두 보관됩니다. 그래도 세팅을 완료할까요?';

const READY_STAGES = new Set(['READY_FOR_ZONES', 'BAKING_TEXTURE', 'COMPLETED']);

/** 이 세션으로 세팅을 완료했는지(서버 setup_completed_at_ms). */
export function setupCompleted(session: SetupSessionResponse | null | undefined): boolean {
  return session?.setup_completed_at_ms != null;
}

/** 완료 버튼을 보일 수 있는 때: 미리보기 맵 이후(구역 단계)이고 아직 완료 전. */
export function canCompleteSetup(session: SetupSessionResponse | null | undefined): boolean {
  return session != null && READY_STAGES.has(session.status) && !setupCompleted(session);
}

/** 적용 결과 한 줄(개수만). */
export function appliedZonesSummary(applied: AppliedZones): string {
  return `구역 적용: 새로 ${applied.created.length} · 고침 ${applied.updated.length} · 유지 ${applied.kept.length} · 보관 ${applied.archived.length}`;
}

function timeOf(ms: number): string {
  return new Date(ms).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
}

export function SetupCompleteCard({ session }: { session: SetupSessionResponse }) {
  const queryClient = useQueryClient();
  const [applied, setApplied] = useState<AppliedZones | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = session.setup_session_id;

  const complete = useMutation({
    mutationFn: () => completeSetupSession(api, id),
    onSuccess: (response) => {
      const { applied_zones: zones, ...view } = response;
      setApplied(zones);
      queryClient.setQueryData(setupSessionKey(id), view);
      void queryClient.invalidateQueries({ queryKey: setupSessionKey(id) });
      // 작업 관리의 진행(SETUP_DONE)·보고서 초안 상태를 다시 받는다
      void queryClient.invalidateQueries({ queryKey: WORK_KEY });
    },
    onError: (e) => {
      setError(setupCompleteErrorMessage(e));
      // 목록의 기존 구역이 그 사이 바뀌었거나 이미 완료됐으면 목록·관제 구역·세션을 다시 받는다
      if (
        e instanceof ApiError &&
        (e.code === 'SETUP_ZONE_STALE' || e.code === 'SETUP_ALREADY_COMPLETED')
      ) {
        void queryClient.invalidateQueries({ queryKey: setupZonePlanKey(id) });
        void queryClient.invalidateQueries({ queryKey: ZONES_KEY });
        void queryClient.invalidateQueries({ queryKey: setupSessionKey(id) });
      }
    },
  });

  const pending = checking || complete.isPending;

  const onClick = async () => {
    if (pending) return;
    setError(null);
    setChecking(true);
    try {
      const plan = await requestSetupZonePlan(api, id);
      if (plan.items.length === 0 && !window.confirm(EMPTY_ZONE_PLAN_CONFIRM)) return;
    } catch (e) {
      setError(setupCompleteErrorMessage(e));
      return;
    } finally {
      setChecking(false);
    }
    complete.mutate();
  };

  if (setupCompleted(session)) {
    return (
      <Card size="sm" data-testid="setup-complete-card">
        <CardHeader>
          <CardTitle>
            <h3>세팅 완료</h3>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-1 text-sm">
          <p role="status" className="font-semibold text-ok">
            {timeOf(session.setup_completed_at_ms!)}에 세팅을 완료했습니다
          </p>
          {applied && (
            <p className="text-xs text-muted-foreground">{appliedZonesSummary(applied)}</p>
          )}
          <p className="text-xs text-muted-foreground">
            이 세팅으로는 구역·맵 보강을 더 고치지 않습니다. 구역 수정은 관제 화면의 구역 편집에서
            하세요.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card size="sm" data-testid="setup-complete-card">
      <CardHeader>
        <CardTitle>
          <h3>세팅 완료</h3>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <p className="text-xs text-muted-foreground">
          구역을 다 고쳤으면 세팅을 완료하세요. 저장한 구역 목록이 관제 구역이 되고 이 세팅의
          맵·보정이 관제에 적용됩니다. 완료는 작업마다 하루 한 번입니다.
        </p>
        <Button
          size="lg"
          disabled={!canCompleteSetup(session) || pending}
          onClick={() => void onClick()}
        >
          {pending ? '완료하는 중…' : '세팅 완료'}
        </Button>
        {error && (
          <Alert role="alert" className="text-xs">
            {error}
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}
