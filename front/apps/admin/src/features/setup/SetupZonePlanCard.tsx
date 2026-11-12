import { saveSetupZonePlan, setupZonePlanKey } from '@/features/setup/api';
import { liveZonesAsItems } from '@/features/setup/setupZonePlan';
import type { SetupSessionResponse } from '@/features/setup/types';
import { useSetupZonePlanQuery } from '@/features/setup/useSetupZonePlan';
import { requestZones, ZONES_KEY } from '@/features/zones/api';
import { commitErrorText } from '@/features/zones/commitErrors';
import { api } from '@/lib/api';
import { Alert } from '@simai/ui/components/alert';
import { Button } from '@simai/ui/components/button';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

// 세팅 구역 단계의 「세팅 구역 목록」 카드(S15P31A101-615). 이 단계의 구역 편집·AI 초안 수락은 관제 구역을 바로 바꾸지 않고 세션 구역
// 목록(-612)에 저장한다. 세팅 완료가 그 목록으로 관제 구역을 맞춘다.
// - 이전 모델 재사용(REUSE): 목록이 비어 있으면(revision 0) 지금 관제 구역으로 한 번 채운다(zone_id 를 이어 쓴다, 같은 World).
// - 새 모델(NEW): 빈 목록에서 시작한다. World 다시 맞추기가 채택됐으면(world_anchor.accepted, 같은 World) 「지금 구역 불러오기」로
//   지금 관제 구역을 이어 쓸 수 있다. World 가 새로 정해졌으면 기존 좌표가 새 3D 와 맞지 않아 불러오기를 끈다(서버 SETUP_ZONE_WORLD_MISMATCH).

/** 지금 관제 구역을 이어 쓸 수 있는 세팅인지(서버 sameWorld 와 같은 규칙). */
export function sameWorldAsLive(session: SetupSessionResponse): boolean {
  return session.mode === 'REUSE' || session.world_anchor?.accepted === true;
}

export function SetupZonePlanCard({ session }: { session: SetupSessionResponse }) {
  const id = session.setup_session_id;
  const queryClient = useQueryClient();
  const plan = useSetupZonePlanQuery(id);
  const [error, setError] = useState<string | null>(null);
  const seeded = useRef<string | null>(null);

  const importLive = useMutation({
    mutationFn: async () => {
      const current = plan.data;
      if (!current) throw new Error('목록을 아직 받지 못했습니다');
      const live = await queryClient.fetchQuery({
        queryKey: ZONES_KEY,
        queryFn: ({ signal }) => requestZones(api, signal),
        staleTime: 0,
      });
      const items = [...current.items, ...liveZonesAsItems(live, current.items)];
      return saveSetupZonePlan(api, id, items, current.revision);
    },
    onSuccess: (saved) => {
      setError(null);
      queryClient.setQueryData(setupZonePlanKey(id), saved);
    },
    onError: (e) => {
      setError(commitErrorText(e));
      void queryClient.invalidateQueries({ queryKey: setupZonePlanKey(id) });
    },
  });

  // 이전 모델 재사용은 처음 들어올 때 빈 목록을 지금 관제 구역으로 채운다(세션마다 한 번)
  const needsSeed =
    session.mode === 'REUSE' &&
    plan.data !== undefined &&
    plan.data.revision === 0 &&
    plan.data.items.length === 0;
  useEffect(() => {
    if (!needsSeed || seeded.current === id || importLive.isPending) return;
    seeded.current = id;
    importLive.mutate();
  }, [needsSeed, id, importLive]);

  const canImport = session.mode !== 'REUSE' && sameWorldAsLive(session);
  const count = plan.data?.items.length ?? null;

  return (
    <Card size="sm" data-testid="setup-zone-plan-card">
      <CardHeader>
        <CardTitle>
          <h3>세팅 구역 목록</h3>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-xs text-muted-foreground">
        <p role="status">
          {count === null
            ? '구역 목록을 받는 중…'
            : `구역 ${count}개 · 세팅을 완료하면 이 목록이 관제 구역이 됩니다`}
        </p>
        {canImport && (
          <Button
            size="sm"
            variant="secondary"
            disabled={plan.data === undefined || importLive.isPending}
            onClick={() => importLive.mutate()}
          >
            지금 구역 불러오기
          </Button>
        )}
        {session.mode !== 'REUSE' && !sameWorldAsLive(session) && (
          <p>
            3D 좌표가 새로 정해진 세팅이라 지금 관제 구역을 불러오지 않습니다. 구역을 새로 그려
            주세요.
          </p>
        )}
        {(error || plan.isError) && (
          <Alert role="alert" className="text-xs">
            {error ?? '세팅 구역 목록을 받지 못했습니다'}
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}
