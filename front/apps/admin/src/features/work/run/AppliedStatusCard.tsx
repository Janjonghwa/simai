import { useCameraStates } from '@/features/cctv/useCameraStates';
import { useCurrentScene } from '@/features/layout/useCurrentScene';
import { appliedStatusLines } from '@/features/work/run/run';
import { useZones } from '@/features/zones/useZones';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { Check } from 'lucide-react';

// 작업 종료 단계 오른쪽 아래 카드: 지금 관제에 적용된 것. 3D 모델은 현재 Scene, 구역은 현장 구역, 카메라는 health 의 실시간 상태다.
// 아직 모르는 값(구역을 못 받음·카메라 상태 없음)은 줄을 감춘다.

export function AppliedStatusCard() {
  const { scene } = useCurrentScene();
  const { zones, raw: zonesRaw } = useZones();
  const { cameras, stateOf } = useCameraStates();
  const lines = appliedStatusLines({
    sceneReady: scene !== null,
    zoneCount: zonesRaw === undefined ? null : zones.length,
    cameras: {
      total: cameras.length,
      live: cameras.filter((camera) => stateOf(camera.id) === 'live').length,
    },
  });
  if (lines.length === 0) return null;
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>관제 적용 상태</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="flex flex-col divide-y divide-border">
          {lines.map((line) => (
            <li key={line} className="flex items-center gap-3 py-3 first:pt-0 last:pb-0">
              <Check aria-hidden className="size-[18px] flex-none text-ok" />
              <span>{line}</span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
