import { cameraLabel } from '@/features/setup/cameraLabels';
import {
  deriveStageRows,
  formatMinSec,
  WAITING_STAGES,
  type HeightCheckRowView,
  type HeightWarningItem,
  type SetupWaitingView,
  type SetupWaitReasonView,
  type WaitingStageRowView,
  type WaitingStepStatus,
} from '@/features/setup/setup';
import { failureGuide } from '@/features/setup/setupFailures';
import {
  CALIBRATE_COLUMN,
  NARROW_COLUMN,
  SetupPreviewSplit,
} from '@/features/setup/SetupPreviewModel';
import { useSmoothProgress } from '@/features/setup/smoothProgress';
import { Button } from '@simai/ui/components/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { TriangleAlert } from 'lucide-react';
import { useEffect, useState } from 'react';

// 세팅 2단계 '보정·생성'. 진행 카드(5단계 막대) · 실패 안내 · 높이 불일치 안내 · 보정 검증 결과 표를 한 열에 쌓고,
// 미리보기 맵이 준비되면(85%) 왼쪽에 3D 미리보기를 둔다(SetupPreviewSplit).
// 막대·단계별 %·'진행 N%' 는 서버 값 사이를 시간으로 채운 부드러운 값이고, 실패 단계와 높이 안내 시점은 서버 값으로만 정한다.
// 부드러운 값은 진행 카드 안에서만 계산해, 값이 바뀌어도 3D 미리보기 칸은 다시 그려지지 않는다.

/** 새 진행 정보 없이 이만큼(초) 지나면 멈춘 것으로 알린다. */
const STALLED_AFTER_SEC = 30;

const VALUE_TONE: Record<WaitingStepStatus, string> = {
  done: 'text-ok',
  current: 'text-foreground',
  upcoming: 'text-muted-foreground',
  failed: 'font-semibold text-delay',
};

function stageValueText(row: WaitingStageRowView): string {
  if (row.status === 'done') return '완료';
  if (row.status === 'upcoming') return '대기';
  if (row.status === 'failed') return '실패';
  return `${Math.round(row.stagePct)}%`;
}

/** 세팅이 왜 기다리는지 한 줄 + 대기 시간 + 할 수 있는 일(S15P31A101-583). */
function WaitReasonNotice({ reason, now }: { reason: SetupWaitReasonView; now: number }) {
  const waited = Math.max(0, Math.floor((now - reason.sinceMs) / 1000));
  const lastSeen =
    reason.lastSeenMs === null
      ? '연결 기록 없음'
      : `마지막 연결 ${formatMinSec(Math.max(0, (now - reason.lastSeenMs) / 1000))} 전`;
  return (
    <div
      role="status"
      aria-label="세팅 대기 이유"
      className={cn(
        'flex flex-col gap-1 rounded-md px-2.5 py-2 text-xs',
        reason.tone === 'delay' ? 'bg-delay/15 text-delay' : 'bg-surface-3 text-muted-foreground',
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold">{reason.title}</span>
        <span className="font-mono">
          대기 {formatMinSec(waited)}
          {reason.showLastSeen ? ` · ${lastSeen}` : ''}
        </span>
      </div>
      {reason.notes.map((note) => (
        <span key={note} className="font-mono text-[11px]">
          {note}
        </span>
      ))}
      <span className="text-foreground">{reason.hint}</span>
    </div>
  );
}

/** 5단계 막대와 진행률·경과 시간. 서버 대기 이유·지연·접수 안내도 이 카드 안에 보인다. */
function ProgressCard({
  waiting,
  pollingError,
}: {
  waiting: SetupWaitingView;
  pollingError: boolean;
}) {
  const shownPct = useSmoothProgress(waiting.progressPct, !waiting.failed);
  const [now, setNow] = useState(() => Date.now());
  const [openedAt] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const elapsed = Math.max(0, Math.floor((now - (waiting.startedAtMs ?? openedAt)) / 1000));
  const unchanged = Math.max(
    0,
    Math.floor((now - (waiting.updatedAtMs ?? waiting.startedAtMs ?? openedAt)) / 1000),
  );
  const stalled = !waiting.failed && !waiting.previewReady && unchanged >= STALLED_AFTER_SEC;
  const percent = Math.round(shownPct);
  const rows = deriveStageRows(shownPct, waiting.failedStageIndex);

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>보정·생성</h2>
        </CardTitle>
        <CardAction className="flex items-baseline gap-2 font-mono text-xs">
          <span
            role="progressbar"
            aria-label="세팅 진행률"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            className="font-semibold text-foreground"
          >
            진행 {percent}%
          </span>
          <span className="text-muted-foreground">경과 {formatMinSec(elapsed)}</span>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3.5">
        {pollingError ? (
          <p role="alert" className="text-xs text-delay">
            서버에서 진행 상태를 가져오지 못했습니다. 연결을 확인해 주세요.
          </p>
        ) : waiting.waitReason ? (
          <WaitReasonNotice reason={waiting.waitReason} now={now} />
        ) : stalled ? (
          <p role="status" className="text-xs text-delay">
            {unchanged}초 동안 새 진행 정보가 없습니다. 계속 유지되면 워커와 배경 수집 상태를 확인해
            주세요.
          </p>
        ) : !waiting.failed && waiting.progressPct === 0 ? (
          <p role="status" className="text-xs text-muted-foreground">
            세팅 요청이 접수됐습니다. 워커의 진행 정보를 기다리는 중입니다.
          </p>
        ) : null}

        <ol aria-label="보정·생성 단계" className="flex flex-col gap-3.5">
          {rows.map((row) => (
            <li key={row.stage} data-status={row.status} className="flex flex-col gap-1.5">
              <div className="flex items-baseline justify-between gap-2 text-sm font-semibold">
                <span className={cn(row.status === 'upcoming' && 'text-muted-foreground')}>
                  {row.label}
                </span>
                <span className={cn('font-mono text-xs font-medium', VALUE_TONE[row.status])}>
                  {stageValueText(row)}
                </span>
              </div>
              <div
                role="progressbar"
                aria-label={`${row.label} 진행률`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(row.stagePct)}
                className="h-1.5 overflow-hidden rounded-full bg-surface-3"
              >
                <div
                  className={cn(
                    'h-full transition-[width] duration-500 ease-out motion-reduce:transition-none',
                    row.status === 'failed' ? 'bg-delay' : 'bg-primary',
                  )}
                  style={{ width: `${Math.round(row.stagePct)}%` }}
                />
              </div>
              {row.index === 0 && row.status === 'current' && (
                <p role="status" className="rounded-md bg-surface-2 px-2.5 py-2 text-xs">
                  배경을 모으는 동안 카메라 앞에서 비켜 주세요.
                </p>
              )}
              {row.index === 0 &&
                (row.status === 'current' || row.status === 'failed') &&
                waiting.backgroundCollection.length > 0 && (
                  <p
                    aria-label="카메라별 배경 수집 진행률"
                    className="font-mono text-xs text-muted-foreground"
                  >
                    {waiting.backgroundCollection
                      .map((cam) => `${cameraLabel(waiting.cameraIds, cam.cameraId)} ${cam.pct}%`)
                      .join(' · ')}
                  </p>
                )}
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}

/** 실패 안내: 어느 단계에서 멈췄는지, 원인, 해결, 그리고 다시 시도(가능할 때)·카메라 연결 관리로 단추. */
function FailureNotice({
  waiting,
  retryPending,
  onRetry,
  onReturnToStep1,
}: {
  waiting: SetupWaitingView;
  retryPending: boolean;
  onRetry: () => void;
  onReturnToStep1: () => void;
}) {
  const guide = failureGuide(waiting.failureCode, waiting.failureMessage, waiting.cameraIds);
  const stage =
    waiting.failedStageIndex === null ? undefined : WAITING_STAGES[waiting.failedStageIndex];
  return (
    <div
      role="alert"
      aria-label="세팅 실패 안내"
      className="flex flex-col gap-1.5 rounded-lg bg-delay/15 px-3.5 py-3 text-sm"
    >
      {stage && (
        <span className="text-xs text-muted-foreground">{stage.label} 단계에서 멈췄어요</span>
      )}
      <span className="font-semibold text-delay">{guide.title}</span>
      <span className="text-foreground">{guide.fix}</span>
      <div className="mt-1 flex flex-wrap gap-2">
        {guide.retry && (
          <Button size="sm" disabled={retryPending} onClick={onRetry}>
            다시 시도
          </Button>
        )}
        <Button size="sm" variant={guide.retry ? 'secondary' : 'default'} onClick={onReturnToStep1}>
          카메라 연결 관리로
        </Button>
      </div>
    </div>
  );
}

/** 카메라 높이 불일치 안내(서버 height_checks·height_warnings 문구를 그대로 보인다). */
function HeightNotice({ warnings }: { warnings: readonly HeightWarningItem[] }) {
  return (
    <div
      role="alert"
      aria-label="카메라 높이 불일치 경고"
      className="flex items-start gap-2.5 rounded-lg bg-delay/15 px-3.5 py-3 text-sm text-delay"
    >
      <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
      <ul className="flex flex-col gap-1">
        {warnings.map((warning) => (
          <li key={warning.cameraId}>{warning.message}</li>
        ))}
      </ul>
    </div>
  );
}

const formatMeters = (meters: number) => `${meters.toFixed(2).replace(/0$/, '')} m`;
const formatDiffCm = (cm: number) => `${Math.abs(cm).toFixed(1)} cm`;

/** 보정 검증 결과: 카메라마다 입력 높이·복원 높이·차이. 맞는지는 서버 판정(within_tolerance)을 그대로 쓴다. */
function ResultCard({
  rows,
  cameraIds,
  toleranceCm,
}: {
  rows: readonly HeightCheckRowView[];
  cameraIds: readonly string[];
  toleranceCm: number | null;
}) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>보정 검증 결과</h2>
        </CardTitle>
        {toleranceCm !== null && (
          <CardAction className="text-xs text-muted-foreground">
            높이 차이 허용 {toleranceCm} cm
          </CardAction>
        )}
      </CardHeader>
      <CardContent>
        <table className="w-full font-mono text-sm tabular-nums">
          <thead>
            <tr className="text-xs text-muted-foreground">
              <th scope="col" className="pb-1 text-left font-normal">
                카메라
              </th>
              <th scope="col" className="pb-1 pl-4 text-right font-normal">
                입력
              </th>
              <th scope="col" className="pb-1 pl-4 text-right font-normal">
                복원
              </th>
              <th scope="col" className="pb-1 pl-4 text-right font-normal">
                차이
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.map((row) => (
              <tr
                key={row.cameraId}
                data-within-tolerance={row.withinTolerance}
                className={cn(!row.withinTolerance && 'text-delay')}
              >
                <th scope="row" className="py-1.5 text-left font-semibold">
                  {cameraLabel(cameraIds, row.cameraId)}
                </th>
                <td className="py-1.5 pl-4 text-right">{formatMeters(row.inputHeightM)}</td>
                <td className="py-1.5 pl-4 text-right">
                  {row.restoredHeightM === null ? '—' : formatMeters(row.restoredHeightM)}
                </td>
                <td
                  className={cn(
                    'py-1.5 pl-4 text-right',
                    row.withinTolerance ? 'text-muted-foreground' : 'font-semibold',
                  )}
                >
                  {row.differenceCm === null ? '—' : formatDiffCm(row.differenceCm)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

export function SetupCalibrateStep({
  waiting,
  pollingError = false,
  retryPending = false,
  retryError = null,
  onRetry,
  onNavigateLiveWork,
  onReturnToStep1,
}: {
  waiting: SetupWaitingView;
  pollingError?: boolean;
  retryPending?: boolean;
  retryError?: string | null;
  onRetry: () => void;
  onNavigateLiveWork?: () => void;
  onReturnToStep1: () => void;
}) {
  const column = (
    <>
      <ProgressCard waiting={waiting} pollingError={pollingError} />
      {waiting.failed && (
        <FailureNotice
          waiting={waiting}
          retryPending={retryPending}
          onRetry={onRetry}
          onReturnToStep1={onReturnToStep1}
        />
      )}
      {retryError && (
        <div
          role="alert"
          data-testid="setup-retry-error"
          className="flex flex-wrap items-center justify-between gap-2 text-xs font-medium text-delay"
        >
          <span>{retryError}</span>
          {onNavigateLiveWork && (
            <Button size="sm" variant="secondary" onClick={onNavigateLiveWork}>
              진행 중인 작업으로 이동
            </Button>
          )}
        </div>
      )}
      {waiting.showHeightNotice && <HeightNotice warnings={waiting.heightWarnings} />}
      {waiting.heightChecks.length > 0 && (
        <ResultCard
          rows={waiting.heightChecks}
          cameraIds={waiting.cameraIds}
          toleranceCm={waiting.heightToleranceCm}
        />
      )}
    </>
  );

  // 미리보기 맵이 준비되기 전에는 진행 카드 하나를 가운데에 둔다. 준비되면 3D 칸이 왼쪽에 생긴다.
  return waiting.previewReady ? (
    <SetupPreviewSplit sceneTier={waiting.sceneTier}>{column}</SetupPreviewSplit>
  ) : (
    <div className={cn(NARROW_COLUMN, CALIBRATE_COLUMN)}>{column}</div>
  );
}
