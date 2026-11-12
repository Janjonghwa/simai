import type { AppliedZones, SetupSessionResponse } from '@/features/setup/types';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';

// 세팅을 끝낸 뒤 구역 단계 오른쪽 칸에 남는 「세팅 완료」 상태 카드(C17, S15P31A101-615). 완료는 구역 단계 아래 오른쪽 단추가
// useCompleteSetup 으로 부른다. 완료 뒤에는 이 세팅으로 구역·보강을 고치지 않는다(하루 1회).

/** 세팅 구역 목록이 비었을 때 완료 전에 묻는 문구(목록에 없는 관제 구역은 보관된다). */
export const EMPTY_ZONE_PLAN_CONFIRM =
  '저장한 구역이 없어 지금 관제 구역이 모두 보관됩니다. 그래도 세팅을 완료할까요?';

const READY_STAGES = new Set(['READY_FOR_ZONES', 'BAKING_TEXTURE', 'COMPLETED']);

/** 이 세션으로 세팅을 완료했는지(서버 setup_completed_at_ms). */
export function setupCompleted(session: SetupSessionResponse | null | undefined): boolean {
  return session?.setup_completed_at_ms != null;
}

/** 세팅을 완료할 수 있는 때: 미리보기 맵 이후(구역 단계)이고 아직 완료 전. */
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

/** 완료한 세션의 상태 카드. 완료 전 세션이면 아무것도 그리지 않는다. applied 는 방금 완료한 이 화면에서만 받는다. */
export function SetupCompleteCard({
  session,
  applied = null,
}: {
  session: SetupSessionResponse;
  applied?: AppliedZones | null;
}) {
  if (session.setup_completed_at_ms == null) return null;
  return (
    <Card size="sm" data-testid="setup-complete-card">
      <CardHeader>
        <CardTitle>
          <h3>세팅 완료</h3>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-1 text-sm">
        <p role="status" className="font-semibold text-ok">
          {timeOf(session.setup_completed_at_ms)}에 세팅을 완료했습니다
        </p>
        {applied && <p className="text-xs text-muted-foreground">{appliedZonesSummary(applied)}</p>}
        <p className="text-xs text-muted-foreground">
          이 세팅으로는 구역·맵 보강을 더 고치지 않습니다. 구역 수정은 관제 화면의 구역 편집에서
          하세요.
        </p>
      </CardContent>
    </Card>
  );
}
