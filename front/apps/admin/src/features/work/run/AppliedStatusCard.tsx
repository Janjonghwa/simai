import { useCameraStates } from '@/features/cctv/useCameraStates';
import { appliedBundle } from '@/features/events/ppePolicy';
import { useCurrentScene } from '@/features/layout/useCurrentScene';
import { monitoringText } from '@/features/monitor/monitoringPhase';
import { MonitoringProblemNote } from '@/features/monitor/MonitoringStatus';
import { useWorkStartPhase } from '@/features/monitor/useMonitoringPhase';
import { useReplayTestAlerts, useTestInput } from '@/features/monitor/useTestInput';
import { appliedStatusLines, type AppliedLine } from '@/features/work/run/run';
import { useRecentBundles } from '@/features/zones/useRecentBundles';
import { Card, CardContent } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { Check, Clock, TriangleAlert } from 'lucide-react';

// 작업 종료 단계 오른쪽 아래 카드: 지금 관제에 적용된 것(S15P31A101-692 에서 실제 적용과 대조). 맨 위 줄은 이번 작업 설정이 현장
// Tier 0 에 적용됐는지(config_ack APPLIED, 경보 켜짐)이고, 구역 수는 그 묶음에 실린 구역, 카메라는 health 의 실시간 상태다.
// 초록 체크는 확인된 것에만 준다. 아직 모르는 값(묶음 목록·카메라 상태 없음)은 줄을 감춘다.
// 녹화 파일 재생(시험 입력) 중이면 맨 위 줄이 「녹화 재생 중 · 실제 알림 꺼짐」이다(S15P31A101-731).

const ICON: Record<AppliedLine['tone'], typeof Check> = {
  ok: Check,
  wait: Clock,
  bad: TriangleAlert,
};

const ICON_COLOR: Record<AppliedLine['tone'], string> = {
  ok: 'text-ok',
  wait: 'text-muted-foreground',
  bad: 'text-sev-caution',
};

export function AppliedStatusCard({
  workId,
  startedAt,
}: {
  workId: string;
  /** 작업 시작 시각(EC2 ms). 적용 지연 판정의 기준이다. */
  startedAt: number | null;
}) {
  const { scene } = useCurrentScene();
  const bundles = useRecentBundles().data;
  const { cameras, stateOf } = useCameraStates();
  const phase = useWorkStartPhase({ work_id: workId, startedAt });
  const testInput = useTestInput();
  const replayTestAlerts = useReplayTestAlerts();
  const applied = bundles ? appliedBundle(bundles) : null;
  const lines = appliedStatusLines({
    phase,
    sceneReady: scene !== null,
    appliedZoneCount: applied
      ? applied.items.filter((item) => item.item_type === 'ZONE').length
      : null,
    cameras: {
      total: cameras.length,
      live: cameras.filter((camera) => stateOf(camera.id) === 'live').length,
    },
    testInput,
    replayTestAlerts,
  });
  if (lines.length === 0) return null;
  const problem =
    lines.find((line) => line.tone === 'bad') ?? lines.find((line) => line.tone === 'wait');
  const zoneCount = applied?.items.filter((item) => item.item_type === 'ZONE').length;
  const live = cameras.filter((camera) => stateOf(camera.id) === 'live').length;
  return (
    <Card size="sm" className="flex-none">
      <details>
        <summary
          className={cn(
            'cursor-pointer px-4 py-3 text-sm font-medium',
            problem ? 'text-sev-caution' : 'text-ok',
          )}
        >
          {problem ? `⚠ ${problem.text}` : '✓ 관제 적용됨'}
          {zoneCount !== undefined && ` · 구역 ${zoneCount}`}
          {cameras.length > 0 &&
            !(problem && problem.text.includes('카메라')) &&
            ` · 카메라 ${live}/${cameras.length}`}
        </summary>
        <CardContent className="flex max-h-[180px] flex-col gap-3 overflow-auto">
          <h2 className="font-semibold">관제 적용 상태</h2>
          <ul className="flex flex-col divide-y divide-border">
            {lines.map((line) => {
              const Icon = ICON[line.tone];
              return (
                <li
                  key={line.text}
                  data-tone={line.tone}
                  className="flex items-center gap-3 py-3 first:pt-0 last:pb-0"
                >
                  <Icon
                    aria-hidden
                    className={cn('size-[18px] flex-none', ICON_COLOR[line.tone])}
                  />
                  <span>{line.text}</span>
                </li>
              );
            })}
          </ul>
          <MonitoringProblemNote text={monitoringText(phase)} />
        </CardContent>
      </details>
    </Card>
  );
}
