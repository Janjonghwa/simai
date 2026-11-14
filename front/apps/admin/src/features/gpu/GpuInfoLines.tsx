import {
  formatElapsed,
  formatUsd,
  gpuErrorReason,
  gpuSummary,
  PREP_STEPS,
  prepStep,
} from '@/features/gpu/gpuStatus';
import { useGpuStatus } from '@/features/gpu/useGpu';
import { useNow } from '@/features/layout/useNow';
import { useTier0Connected } from '@/hooks/useTier0Connected';
import { isLagHigh, LAG_WINDOW_MS } from '@/lib/tier0Lag';
import { selectTier0LagMs, useRealtimeStore } from '@/store/realtime.store';
import { cn } from '@simai/ui/lib/utils';

// GPU 정보 줄(S15P31A101-702): GPU 종류·지역, 경과·예상 비용, 판정 지연(-704 의 상단바 「판정 지연」을 여기로 옮김),
// 준비 중이면 단계(켜는 중 → 가중치 → 엔진 → 준비 완료), 오류면 까닭. 모르는 줄은 뺀다(지어내지 않음).
// 상단바 점의 마우스 올림 정보창과 켜기·끄기 패널이 함께 쓴다.

export function GpuInfoLines() {
  const { status, clockOffsetMs } = useGpuStatus();
  const now = useNow();
  const summary = gpuSummary(status, now + clockOffsetMs);
  const step = prepStep(status);
  const reason = gpuErrorReason(status);
  return (
    <div className="flex flex-col gap-1 text-sm">
      {summary && (
        <>
          <span>
            {summary.gpuType}
            {summary.region ? ` · ${summary.region}` : ''}
          </span>
          <span className="text-muted-foreground">
            경과 {formatElapsed(summary.elapsedMs)}
            {summary.costUsd !== null && ` · 약 ${formatUsd(summary.costUsd)}`}
          </span>
        </>
      )}
      <Tier0LagLine />
      {step !== null && (
        <ol aria-label="준비 단계" className="flex flex-wrap items-center gap-1 text-xs">
          {PREP_STEPS.map((name, i) => (
            <li
              key={name}
              aria-current={i === step ? 'step' : undefined}
              className={cn(i === step ? 'font-semibold' : 'text-muted-foreground')}
            >
              {i > 0 && <span aria-hidden> → </span>}
              {name}
            </li>
          ))}
        </ol>
      )}
      {reason && <span>{reason}</span>}
    </div>
  );
}

/**
 * 판정 지연(S15P31A101-665, 이름은 -704, 자리는 -702 의 GPU 정보창). Tier 0 가 자기 시계로 잰 T_lag 의 최근 몇 초 평균이다.
 * Tier 0 가 이어지지 않았거나 Tier 0 가 모른다고 하면 줄을 뺀다. 기준(명세 6.1 DEGRADED) 이상이면 지연 색이다.
 */
export function Tier0LagLine() {
  const connected = useTier0Connected();
  const lag = useRealtimeStore(selectTier0LagMs);
  if (!connected || lag === undefined) return null;
  return (
    <span
      title={`Tier 0 가 잰 촬영→판정 처리 지연, 최근 ${LAG_WINDOW_MS / 1000}초 평균`}
      className={cn(
        'font-mono',
        isLagHigh(lag) ? 'font-semibold text-delay' : 'text-muted-foreground',
      )}
    >
      판정 지연 {lag} ms
    </span>
  );
}
