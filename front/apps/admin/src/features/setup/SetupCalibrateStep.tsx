import {
  cameraCheckFailed,
  cameraCheckGuide,
  type CameraCheckView,
  type PairOutcome,
} from '@/features/setup/cameraCheck';
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
import { SetupPreviewCard, SetupSplit } from '@/features/setup/SetupPreviewModel';
import { Button } from '@simai/ui/components/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { TriangleAlert } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';

// 세팅 2단계 '보정·생성'. 1단계와 같은 두 칸 배치(SetupSplit)로, 오른쪽에 진행 카드(5단계 막대) · 실패·멈춤 안내 · 카메라 확인 ·
// 「자세히」(높이 불일치 안내·보정 검증 결과 표, -690 부터 접어 둔다)를 쌓는다. 높이 확인을 기다리는 멈춤은 높이 확인 창이 맡는다. 왼쪽은 미리보기 맵이 준비되기 전(85%)에는 1단계의 카메라 시야·실시간 영상, 준비되면 3D 미리보기다.
// 막대·단계별 %·'진행 N%' 는 서버 값만 쓴다. 경과 시간만 진행 카드 안에서 갱신한다.

/** 새 진행 정보 없이 이만큼(초) 지나면 멈춘 것으로 알린다. */
const STALLED_AFTER_SEC = 30;

const VALUE_TONE: Record<WaitingStepStatus, string> = {
  done: 'text-ok',
  current: 'text-foreground',
  upcoming: 'text-muted-foreground',
  failed: 'font-semibold text-delay',
  paused: 'font-semibold text-delay',
};

function stageValueText(row: WaitingStageRowView): string {
  if (row.status === 'done') return '완료';
  if (row.status === 'upcoming') return '대기';
  if (row.status === 'failed') return '실패';
  if (row.status === 'paused') return '멈춤';
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
  const stalled =
    !waiting.failed &&
    waiting.pausedStageIndex === null &&
    !waiting.previewReady &&
    unchanged >= STALLED_AFTER_SEC;
  const percent = waiting.progressPct;
  const rows = deriveStageRows(
    waiting.progressPct,
    waiting.failedStageIndex,
    waiting.pausedStageIndex,
  );
  const currentIndex = rows.find((row) => row.status === 'current')?.index;
  const stageKey = `${waiting.sessionId}:${currentIndex}`;
  const enteredAt = (currentIndex === 0 ? waiting.startedAtMs : waiting.updatedAtMs) ?? openedAt;
  const [stageClock, setStageClock] = useState({ key: stageKey, since: enteredAt });
  if (stageClock.key !== stageKey) {
    // 같은 단계의 하위 진행·카메라 확인 updated_at 은 경과 시간을 초기화하지 않는다.
    setStageClock({ key: stageKey, since: waiting.updatedAtMs ?? now });
  }
  const stageElapsed = Math.max(0, Math.floor((now - stageClock.since) / 1000));

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
                  {row.status === 'current' && (
                    <span className="mr-2 inline-flex items-center gap-1 text-muted-foreground">
                      <span
                        aria-hidden="true"
                        className="size-1.5 animate-pulse rounded-full bg-primary motion-reduce:animate-none"
                      />
                      진행 중 · {formatMinSec(stageElapsed)} 경과
                    </span>
                  )}
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
                    row.status === 'failed' || row.status === 'paused' ? 'bg-delay' : 'bg-primary',
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

/**
 * 멈춤 안내: 보정에 문제가 있어 모델 생성을 멈췄다(서버 paused_reason). 원인 문장은 품질 게이트 문제, 없으면 높이 불일치 문구다.
 * 카메라 연결 관리에서 고치고 '다음' 을 누르면 같은 세션이 멈춘 곳부터 이어 간다.
 */
function PausedNotice({
  stageIndex,
  problems,
  onReturnToStep1,
}: {
  stageIndex: number;
  problems: readonly string[];
  onReturnToStep1: () => void;
}) {
  const stage = WAITING_STAGES[stageIndex];
  return (
    <div
      role="alert"
      aria-label="세팅 멈춤 안내"
      className="flex flex-col gap-1.5 rounded-lg bg-delay/15 px-3.5 py-3 text-sm"
    >
      <span className="font-semibold text-delay">
        {stage ? `${stage.label}에서 멈췄어요` : '모델 생성을 멈췄어요'}
      </span>
      {problems.map((problem) => (
        <span key={problem} className="text-foreground">
          {problem}
        </span>
      ))}
      <span className="text-foreground">고친 뒤 다음을 누르면 멈춘 곳부터 이어서 만듭니다.</span>
      <div className="mt-1">
        <Button size="sm" onClick={onReturnToStep1}>
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

const OUTCOME_TEXT: Record<PairOutcome, string> = {
  pass: '통과',
  fail: '실패',
  pending: '확인 중',
  na: '—',
};

const OUTCOME_TONE: Record<PairOutcome, string> = {
  pass: 'text-ok',
  fail: 'font-semibold text-delay',
  pending: 'text-muted-foreground',
  na: 'text-muted-foreground',
};

const formatPx = (median: number | null, p90: number | null) =>
  median === null ? '—' : `${median.toFixed(2)}${p90 === null ? '' : ` / ${p90.toFixed(2)}`}`;
const formatRate = (rate: number | null) => (rate === null ? '—' : `${Math.round(rate * 100)}%`);

/**
 * 쌍별 검증(S15P31A101-646): 옆 카메라 4쌍마다 세팅 보정의 배경 홀드아웃 오차(중앙값/p90 px)와 카메라 확인의 사람 통과율, 종합 판정.
 * 관제의 쌍 연결 검사와 같은 계산이라, 여기서 실패한 쌍은 관제에서 같은 사람을 둘로 가르기 쉽다.
 */
function PairResultCard({ view }: { view: CameraCheckView }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>쌍별 검증</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <table aria-label="카메라 쌍별 검증" className="w-full font-mono text-sm tabular-nums">
          <thead>
            <tr className="text-xs text-muted-foreground">
              <th scope="col" className="pb-1 text-left font-normal">
                쌍
              </th>
              <th scope="col" className="pb-1 pl-3 text-right font-normal">
                보정 오차 px
              </th>
              <th scope="col" className="pb-1 pl-3 text-right font-normal">
                사람 통과율
              </th>
              <th scope="col" className="pb-1 pl-3 text-right font-normal">
                판정
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {view.pairs.map((row) => (
              <tr key={row.key} data-outcome={row.outcome}>
                <th scope="row" className="py-1.5 text-left font-semibold">
                  {row.label}
                </th>
                <td
                  className={cn(
                    'py-1.5 pl-3 text-right',
                    row.holdoutVerdict === 'FAIL' ? 'text-delay' : 'text-muted-foreground',
                  )}
                >
                  {formatPx(row.holdoutMedianPx, row.holdoutP90Px)}
                </td>
                <td
                  className={cn(
                    'py-1.5 pl-3 text-right',
                    row.checkVerdict === 'FAIL' ? 'text-delay' : 'text-muted-foreground',
                  )}
                >
                  {formatRate(row.passRate)}
                </td>
                <td className={cn('py-1.5 pl-3 text-right', OUTCOME_TONE[row.outcome])}>
                  {OUTCOME_TEXT[row.outcome]}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

const CHECK_STATUS_TEXT: Record<CameraCheckView['status'], string> = {
  NONE: '대기',
  WAITING: '사람 기다리는 중',
  MEASURING: '확인 중',
  PASSED: '통과',
  FAILED: '실패',
  INCOMPLETE: '못 마침',
};

/**
 * 카메라 확인(S15P31A101-646): 보정 뒤 한 사람이 30~60초 걷는 동안 관제와 같은 검사(쌍 연결·한 카메라 제외 재투영·4대 관측)를
 * 새 보정으로 잰다. 통과해야 구역 단계로 간다. 실패하면 원인과 '다시 보정' 을 보인다.
 */
function CameraCheckCard({
  view,
  onRecalibrate,
}: {
  view: CameraCheckView;
  onRecalibrate: () => void;
}) {
  const failed = cameraCheckFailed(view);
  const guide = cameraCheckGuide(view);
  const percent = view.passed ? 100 : Math.round(view.progressPct);
  return (
    <Card size="sm" aria-label="카메라 확인">
      <CardHeader>
        <CardTitle>
          <h2>카메라 확인</h2>
        </CardTitle>
        <CardAction
          className={cn(
            'font-mono text-xs',
            view.passed ? 'text-ok' : failed ? 'font-semibold text-delay' : 'text-muted-foreground',
          )}
        >
          {view.passed ? '통과' : CHECK_STATUS_TEXT[view.status]}
          {view.elapsedS > 0 && ` · ${Math.round(view.elapsedS)}초`}
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {guide && (
          <p role="status" className={cn('text-sm', failed ? 'text-delay' : 'text-foreground')}>
            {guide}
          </p>
        )}
        {!failed && (
          <div
            role="progressbar"
            aria-label="카메라 확인 진행률"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            className="h-1.5 overflow-hidden rounded-full bg-surface-3"
          >
            <div
              className={cn(
                'h-full transition-[width] duration-500 ease-out motion-reduce:transition-none',
                view.passed ? 'bg-ok' : 'bg-primary',
              )}
              style={{ width: `${percent}%` }}
            />
          </div>
        )}
        {view.cameras.length > 0 && (
          <ul
            aria-label="카메라별 확인"
            className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-xs"
          >
            {view.cameras.map((cam) => (
              <li
                key={cam.cameraId}
                className={cn(
                  cam.verdict === 'PASS'
                    ? 'text-ok'
                    : cam.verdict === 'FAIL'
                      ? 'font-semibold text-delay'
                      : 'text-muted-foreground',
                )}
              >
                {cam.label}{' '}
                {cam.verdict === 'NOT_OBSERVED' ? '사람 안 보임' : `${cam.observedViews}회`}
                {cam.looMedianPx !== null && ` · ${cam.looMedianPx.toFixed(1)}px`}
              </li>
            ))}
          </ul>
        )}
        {view.problems.length > 0 && (
          <ul
            role="alert"
            aria-label="카메라 확인 문제"
            className="flex flex-col gap-1 text-xs text-delay"
          >
            {view.problems.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        )}
        {failed && (
          <div>
            <Button size="sm" onClick={onRecalibrate}>
              다시 보정
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function SetupCalibrateStep({
  waiting,
  cameraView,
  cameraCheck,
  showCameraCheck = false,
  onRecalibrate,
  pollingError = false,
  retryPending = false,
  retryError = null,
  onRetry,
  onNavigateLiveWork,
  onReturnToStep1,
}: {
  waiting: SetupWaitingView;
  /** 미리보기 맵이 준비되기 전 왼쪽 칸(1단계의 카메라 시야·실시간 영상). */
  cameraView: ReactNode;
  /** 보정 품질 게이트·카메라 확인(S15P31A101-646). 없으면 쌍별 표·카메라 확인 카드를 그리지 않는다. */
  cameraCheck?: CameraCheckView;
  /** 보정이 등록돼 카메라 확인 카드를 보일 때(실패 세션 제외). */
  showCameraCheck?: boolean;
  onRecalibrate?: () => void;
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
      {waiting.pausedStageIndex !== null && !waiting.heightCheckPending && (
        <PausedNotice
          stageIndex={waiting.pausedStageIndex}
          problems={
            cameraCheck?.problems.length
              ? cameraCheck.problems
              : waiting.heightWarnings.map((warning) => warning.message)
          }
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
      {cameraCheck && showCameraCheck && (
        <CameraCheckCard view={cameraCheck} onRecalibrate={onRecalibrate ?? onReturnToStep1} />
      )}
      {(waiting.showHeightNotice || waiting.heightChecks.length > 0) && (
        // 높이는 3D 공간을 만든 직후 높이 확인 창에서 이미 확인했다(-690). 거치대 문구·복원 높이 표는 접어 둔다
        <details aria-label="카메라 높이 자세히" className="rounded-xl bg-card px-3.5 py-2.5">
          <summary className="cursor-pointer text-xs font-semibold text-muted-foreground">
            자세히 · 카메라 높이
          </summary>
          <div className="mt-2.5 flex flex-col gap-2.5">
            {waiting.showHeightNotice && <HeightNotice warnings={waiting.heightWarnings} />}
            {waiting.heightChecks.length > 0 && (
              <ResultCard
                rows={waiting.heightChecks}
                cameraIds={waiting.cameraIds}
                toleranceCm={waiting.heightToleranceCm}
              />
            )}
          </div>
        </details>
      )}
      {cameraCheck && (showCameraCheck || cameraCheck.qualityVerdict !== null) && (
        <PairResultCard view={cameraCheck} />
      )}
    </>
  );

  return (
    <SetupSplit
      left={waiting.previewReady ? <SetupPreviewCard sceneTier={waiting.sceneTier} /> : cameraView}
    >
      {column}
    </SetupSplit>
  );
}
