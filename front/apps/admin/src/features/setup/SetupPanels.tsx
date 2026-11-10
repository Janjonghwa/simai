import { tileLabel } from '@/features/cctv/cctv';
import { useCctvStore } from '@/features/cctv/cctv.store';
import { DIMMED, TILE_FRAME } from '@/features/cctv/tileStyle';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { ROOM } from '@/features/plan/plan';
import {
  CAMERA_HEIGHT_HELP,
  CANDIDATE_KIND_LABEL,
  formatMinSec,
  MAP_TITLE,
  MAX_CAMERA_HEIGHT_M,
  type CoverageCameraView,
  type HeightFieldView,
  type HeightWarningItem,
  type SceneTierStatusView,
  type SetupTone,
  type SetupWaitingView,
  type SetupWaitReasonView,
  type Step1CoverageView,
} from '@/features/setup/setup';
import { SetupMap } from '@/features/setup/SetupMap';
import type { SetupZoneDraftsResponse, ZoneDraftItem } from '@/features/setup/types';
import type { DraftAcceptFailure } from '@/features/setup/useAcceptZoneDraft';
import { DEFAULT_CCTV_PREVIEW_URL } from '@/lib/parseEnv';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { useCallback, useEffect, useState, type ReactNode } from 'react';

// 세팅 화면 패널: 1단계 2D 지도 양옆의 카메라 영상(기존 디코딩 재사용)·카메라 연결/방향 안내·카메라 높이 입력, 이후 단계 요약.
// 색은 위험 등급색(sev-critical·sev-high·sev-caution)과 겹치지 않는 토큰만 쓴다.

const DOT: Record<SetupTone, string> = {
  ok: 'bg-ok',
  active: 'bg-primary',
  delay: 'bg-delay',
  idle: 'bg-faint',
};

const TONE_BADGE: Record<SetupTone, string> = {
  ok: 'bg-ok/15 text-ok',
  active: 'bg-primary/15 text-point-soft',
  delay: 'bg-delay/15 text-delay',
  idle: 'bg-surface-3 text-muted-foreground',
};

const CAPTION =
  'absolute flex items-center gap-2 rounded-md bg-card/80 px-2 py-1 text-xs backdrop-blur-[4px]';

function readPreviewUrls() {
  const raw = import.meta.env as Record<string, unknown>;
  const localUrl =
    typeof raw.VITE_CCTV_PREVIEW_URL === 'string' && raw.VITE_CCTV_PREVIEW_URL.trim() !== ''
      ? raw.VITE_CCTV_PREVIEW_URL.trim()
      : DEFAULT_CCTV_PREVIEW_URL;
  const stompUrl =
    typeof raw.VITE_STOMP_URL === 'string' && raw.VITE_STOMP_URL.trim() !== ''
      ? raw.VITE_STOMP_URL.trim()
      : 'ws://localhost:8080/ws';
  return { localUrl, stompUrl };
}

function Dot({ tone }: { tone: SetupTone }) {
  return <i aria-hidden className={cn('size-2 flex-none rounded-full', DOT[tone])} />;
}

function Panel({
  title,
  children,
  dense = false,
  fill = 0,
}: {
  title: string;
  children: ReactNode;
  /** 세로 공간이 모자랄 때: 안쪽 간격을 줄인다. */
  dense?: boolean;
  /**
   * 놓인 칸의 남은 높이를 나눠 갖는 비율(같은 칸의 다른 패널과의 비). 0 이면 내용 높이 그대로다. 내용보다 작아지지는 않는다.
   */
  fill?: number;
}) {
  return (
    <Card
      size="sm"
      className={cn(fill > 0 && 'shrink-0')}
      style={fill > 0 ? { flexGrow: fill } : undefined}
    >
      <CardHeader>
        <CardTitle>
          <h2>{title}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className={cn('flex flex-col', dense ? 'gap-2' : 'gap-3', fill > 0 && 'grow')}>
        {children}
      </CardContent>
    </Card>
  );
}

function Rows({ children }: { children: ReactNode }) {
  return <ul className="flex flex-1 flex-col divide-y divide-border">{children}</ul>;
}

function Row({ children }: { children: ReactNode }) {
  return <li className="flex flex-1 items-center gap-2.5 px-0.5 py-1 text-sm">{children}</li>;
}

/** 영상 칸 비율: 보통 16:9 이고, 폭이 모자라면 4:3 까지 좁아진다(영상은 칸 안에 비율대로 담겨 위아래에 빈 띠가 생긴다). */
const TILE_ASPECT = 16 / 9;
const TILE_MIN_ASPECT = 4 / 3;
/** 같은 쪽 영상 두 칸 사이 간격과, 영상 열·지도 사이 간격(px). */
const TILE_GAP_PX = 12;
const SIDE_GAP_PX = 16;
/** 영상 칸 높이 = (지도 높이 − 칸 간격) / 2. 위쪽 칸은 지도 위 끝, 아래쪽 칸은 지도 아래 끝에 맞는다. */
const TILE_HEIGHT = `calc((var(--h) - ${TILE_GAP_PX}px) / 2)`;
/** 영상 열 폭: 16:9 로 맞추되 지도와 양옆 열이 컨테이너에 들어가는 폭을 넘지 않는다. */
const TILE_COLUMN_WIDTH = `min(calc(${TILE_HEIGHT} * ${TILE_ASPECT}), calc((100cqw - var(--h) - ${2 * SIDE_GAP_PX}px) / 2))`;
/** 영상 열이 가장 좁은 4:3 일 때 전체 폭 = 지도 높이 × (1 + 4/3) − 칸 간격 × 4/3 + 열 간격 2개. */
const MIN_ASPECT_FIXED_WIDTH_PX = 2 * SIDE_GAP_PX - TILE_GAP_PX * TILE_MIN_ASPECT;
/** 지도 높이: 카드 안 남은 높이에 맞추되, 그 폭에 영상 두 열(최소 4:3)과 지도가 들어가는 높이를 넘지 않는다. */
const MAP_HEIGHT = `min(100cqh, calc((100cqw - ${MIN_ASPECT_FIXED_WIDTH_PX}px) / ${1 + TILE_MIN_ASPECT}))`;

/** 카메라 영상 한 칸(16:9). 영상이 오지 않을 때는 흐리게 두고 이유를 글로 보인다. */
function CameraTile({
  camera,
  attach,
}: {
  camera: CoverageCameraView;
  attach: (cameraId: string, canvas: HTMLCanvasElement | null) => void;
}) {
  const phase = useCctvStore((s) => s.views[camera.id]?.phase);
  const link = useCctvStore((s) => s.link);
  const playing = link === 'open' && phase === 'playing';
  const ref = useCallback(
    (canvas: HTMLCanvasElement | null) => attach(camera.id, canvas),
    [attach, camera.id],
  );
  return (
    <div className="w-full" style={{ height: TILE_HEIGHT }}>
      <div className={cn(TILE_FRAME, 'bg-surface-2')}>
        <canvas
          ref={ref}
          aria-label={`${camera.id} 영상`}
          className={cn('size-full object-contain', !playing && DIMMED)}
        />
        <span className={cn(CAPTION, 'top-2 left-2')}>
          <Dot tone={camera.tone} />
          <span className="font-mono font-semibold">{camera.id}</span>
          <span>{tileLabel(phase, link)}</span>
        </span>
      </div>
    </div>
  );
}

/** 지도 한쪽 옆의 영상 칸들. 카메라가 지도 위쪽 모서리에 있으면 위, 아래쪽 모서리에 있으면 아래에 붙는다. */
function CameraTileColumn({
  label,
  cameras,
  attach,
}: {
  label: string;
  cameras: readonly CoverageCameraView[];
  attach: (cameraId: string, canvas: HTMLCanvasElement | null) => void;
}) {
  return (
    <div role="group" aria-label={label} className="flex flex-col justify-between gap-3">
      {cameras.map((camera) => (
        <div
          key={camera.id}
          className={cn(cameras.length === 1 && camera.corner[1] < ROOM.depth / 2 && 'mt-auto')}
        >
          <CameraTile camera={camera} attach={attach} />
        </div>
      ))}
    </div>
  );
}

/** 모서리 위치로 나눈 카메라 목록(왼쪽·오른쪽, 각각 위에서 아래로). */
function camerasBySide(cameras: readonly CoverageCameraView[]) {
  const top = (a: CoverageCameraView, b: CoverageCameraView) => b.corner[1] - a.corner[1];
  const isLeft = (camera: CoverageCameraView) => camera.corner[0] < ROOM.width / 2;
  return {
    left: cameras.filter(isLeft).sort(top),
    right: cameras.filter((camera) => !isLeft(camera)).sort(top),
  };
}

/**
 * 1단계 2D 지도와 카메라 영상을 한 카드로 묶는다. 지도는 가운데, 카메라 영상은 그 카메라가 있는 모서리 쪽 양옆에 둔다
 * (위쪽 카메라 영상은 지도 위 끝, 아래쪽 카메라 영상은 지도 아래 끝에 맞춘다). 카드는 놓인 칸의 높이를 꽉 채우고, 지도는
 * 그 안에서 가능한 크게 가운데에 둔다. 영상이 오지 않는 칸은 흐리게 두고 상태를 글로 보인다.
 */
export function CameraMapPanel({ coverage }: { coverage: Step1CoverageView }) {
  const { localUrl, stompUrl } = readPreviewUrls();
  const { attach } = useCctvPreview(localUrl, stompUrl);
  const { left, right } = camerasBySide(coverage.cameras);
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>{MAP_TITLE[0]}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="[container-type:size] min-h-0 flex-1">
        <div
          className="flex size-full items-center justify-center"
          style={{ gap: SIDE_GAP_PX, ['--h' as string]: MAP_HEIGHT }}
        >
          <div style={{ height: 'var(--h)', width: TILE_COLUMN_WIDTH }}>
            <CameraTileColumn label="왼쪽 카메라 영상" cameras={left} attach={attach} />
          </div>
          <div style={{ height: 'var(--h)', width: 'var(--h)' }}>
            <SetupMap stage={0} coverage={coverage} className="h-full max-h-none" />
          </div>
          <div style={{ height: 'var(--h)', width: TILE_COLUMN_WIDTH }}>
            <CameraTileColumn label="오른쪽 카메라 영상" cameras={right} attach={attach} />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

/** 1단계 카메라별 수신 상태·회전 방향 안내. 카메라 쌍 등급은 2D 지도의 연결선에 보인다. 칸이 남으면 행 사이 여백이 고르게 늘어난다. */
export function CameraPanel({ coverage }: { coverage: Step1CoverageView }) {
  return (
    <Panel title="카메라 연결 · 방향 안내" dense fill={2}>
      <Rows>
        {coverage.cameras.map((cam) => (
          <Row key={cam.id}>
            <Dot tone={cam.tone} />
            <b className="font-mono">{cam.id}</b>
            <Badge
              className={cn(
                'h-auto rounded px-1.5 py-0.5 font-mono text-xs font-medium',
                TONE_BADGE[cam.tone],
              )}
            >
              {cam.statusLabel}
            </Badge>
            <span className="ml-auto text-right text-xs text-muted-foreground">
              {cam.angleLabel}
            </span>
          </Row>
        ))}
      </Rows>
    </Panel>
  );
}

/** 1단계 카메라별 실측 높이(바닥~렌즈 중심, m) 입력 패널. */
export function CameraHeightPanel({
  fields,
  onChange,
  submitError,
  onNavigateLiveWork,
  heightWarnings = [],
  retrySessionId = null,
  onRetryWithHeights,
  retryPending = false,
  canSubmitHeights = true,
}: {
  fields: readonly HeightFieldView[];
  onChange: (cameraId: string, value: string) => void;
  submitError: string | null;
  onNavigateLiveWork?: () => void;
  heightWarnings?: readonly HeightWarningItem[];
  retrySessionId?: string | null;
  onRetryWithHeights?: () => void;
  retryPending?: boolean;
  canSubmitHeights?: boolean;
}) {
  const warnedCamIds = new Set(heightWarnings.map((w) => w.cameraId));
  return (
    <Panel title="카메라 높이 입력 (바닥~렌즈 중심, m)" dense fill={1}>
      <p className="text-xs text-muted-foreground">{CAMERA_HEIGHT_HELP}</p>
      {heightWarnings.length > 0 && (
        <div
          role="alert"
          aria-label="카메라 높이 불일치 수정 안내"
          className="flex flex-col gap-1.5 rounded-lg bg-delay/15 px-3 py-2 text-xs text-delay"
        >
          <p className="font-semibold">
            높이 불일치 카메라: {heightWarnings.map((w) => w.cameraId).join(', ')}
          </p>
          {heightWarnings.map((w) => (
            <p key={w.cameraId} className="font-mono text-[11px]">
              {w.message}
            </p>
          ))}
        </div>
      )}
      <div className="grid flex-1 auto-rows-fr grid-cols-2 gap-2">
        {fields.map((field) => {
          const inputId = `camera-height-${field.cameraId}`;
          const needsInput = field.required || field.missing;
          const isWarned = warnedCamIds.has(field.cameraId);
          return (
            <div
              key={field.cameraId}
              className={cn(
                'flex flex-col justify-center gap-1 rounded-lg bg-surface-2 px-3 py-1.5',
                isWarned && 'ring-1 ring-delay',
              )}
            >
              <div className="flex items-center justify-between gap-2 text-xs">
                <label htmlFor={inputId} className="font-mono font-semibold text-foreground">
                  {field.cameraId} 높이 (m)
                </label>
                <span
                  className={cn(
                    'text-[11px]',
                    field.missing || field.invalid || isWarned
                      ? 'font-semibold text-delay'
                      : 'text-muted-foreground',
                  )}
                >
                  {field.missing
                    ? '입력 필수'
                    : field.invalid
                      ? `0~${MAX_CAMERA_HEIGHT_M}m 입력`
                      : isWarned
                        ? '높이 재확인 필요'
                        : field.required
                          ? '신규 입력'
                          : '지난 제출값'}
                </span>
              </div>
              <input
                id={inputId}
                type="number"
                inputMode="decimal"
                step="0.01"
                min="0.01"
                max={MAX_CAMERA_HEIGHT_M}
                required={needsInput}
                aria-invalid={field.missing || field.invalid || isWarned}
                placeholder="예: 2.70"
                value={field.rawValue}
                onChange={(e) => onChange(field.cameraId, e.target.value)}
                className={cn(
                  'h-7 w-full rounded-md border bg-card px-2.5 font-mono text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  field.missing || field.invalid || isWarned ? 'border-delay' : 'border-border',
                )}
              />
            </div>
          );
        })}
      </div>
      {retrySessionId && onRetryWithHeights && (
        <div className="flex items-center justify-end gap-2">
          <Button
            size="sm"
            disabled={!canSubmitHeights || retryPending}
            onClick={onRetryWithHeights}
          >
            다시 시도
          </Button>
        </div>
      )}
      {submitError && (
        <div
          role="alert"
          data-testid="setup-submit-error"
          className="flex flex-wrap items-center justify-between gap-2 text-xs font-medium text-delay"
        >
          <span>{submitError}</span>
          {onNavigateLiveWork && (
            <Button size="sm" variant="secondary" onClick={onNavigateLiveWork}>
              진행 중인 작업으로 이동
            </Button>
          )}
        </div>
      )}
    </Panel>
  );
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

/** 2단계 대기 화면 패널(배경 수집 → 3D 공간·좌표계 → 카메라 정밀 보정 → 미리보기 맵·구역 초안, 진행률·남은 시간·실패/높이 경고). */
export function WaitingPanel({
  waiting,
  pollingError = false,
  onRetry,
  retryPending = false,
  retryError = null,
  onNavigateLiveWork,
  onReturnToStep1,
}: {
  waiting: SetupWaitingView;
  pollingError?: boolean;
  onRetry?: () => void;
  retryPending?: boolean;
  retryError?: string | null;
  onNavigateLiveWork?: () => void;
  onReturnToStep1?: () => void;
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
  const elapsedLabel = `${Math.floor(elapsed / 60)}분 ${String(elapsed % 60).padStart(2, '0')}초`;
  const stalled = !waiting.failed && !waiting.previewReady && unchanged >= 30;
  return (
    <Panel title="현장 자동 보정 · 3D 공간 생성 대기" fill={1}>
      <div className="flex flex-col gap-2 rounded-lg bg-surface-2 px-3.5 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="font-semibold text-foreground">{waiting.stageLabel}</span>
          <span className="font-mono text-muted-foreground" aria-label="세팅 진행률 및 경과 시간">
            실제 진행률 <strong className="text-foreground">{waiting.progressPct}%</strong> · 경과
            시간 <strong className="text-foreground">{elapsedLabel}</strong>
          </span>
        </div>
        <div
          role="progressbar"
          aria-label="세팅 진행률"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={waiting.progressPct}
          className="h-2 w-full overflow-hidden rounded-full bg-surface-3"
        >
          <div
            className={cn('h-full transition-all', waiting.failed ? 'bg-delay' : 'bg-primary')}
            style={{ width: `${Math.max(0, Math.min(100, waiting.progressPct))}%` }}
          />
        </div>
        <p className="text-[11px] text-muted-foreground">
          서버에서 받은 진행률입니다 · 2초마다 상태를 확인합니다
        </p>
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
      </div>

      <ol aria-label="세팅 대기 세부 단계" className="divide-y divide-border">
        {waiting.steps.map((step) => {
          const tone: SetupTone =
            step.status === 'done'
              ? 'ok'
              : step.status === 'current'
                ? 'active'
                : step.status === 'failed'
                  ? 'delay'
                  : 'idle';
          const statusText =
            step.status === 'done'
              ? '완료'
              : step.status === 'current'
                ? '진행 중'
                : step.status === 'failed'
                  ? '실패'
                  : '대기';
          return (
            <li key={step.stage} className="flex items-start gap-2.5 py-2 text-sm">
              <Dot tone={tone} />
              <div className="flex flex-col gap-0.5">
                <span className="font-medium text-foreground">{step.label}</span>
                <span className="text-xs text-muted-foreground">{step.description}</span>
              </div>
              <Badge
                className={cn(
                  'ml-auto h-auto rounded px-1.5 py-0.5 font-mono text-xs font-semibold',
                  TONE_BADGE[tone],
                )}
              >
                {statusText}
              </Badge>
            </li>
          );
        })}
      </ol>

      {waiting.backgroundCollection.length > 0 && (
        <div className="mt-auto grid grid-cols-4 gap-2" aria-label="카메라별 배경 수집 진행률">
          {waiting.backgroundCollection.map((cam) => (
            <div
              key={cam.cameraId}
              className="flex flex-col gap-1.5 rounded-md bg-surface-2 px-2.5 py-1.5 font-mono text-xs"
            >
              <div className="flex items-center justify-between gap-1">
                <span>{cam.cameraId}</span>
                <span className="font-semibold text-foreground">{cam.pct}%</span>
              </div>
              <div
                role="progressbar"
                aria-label={`${cam.cameraId} 배경 수집률`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.max(0, Math.min(100, cam.pct))}
                className="h-1.5 overflow-hidden rounded-full bg-surface-3"
              >
                <div
                  className="h-full bg-primary transition-all"
                  style={{ width: `${Math.max(0, Math.min(100, cam.pct))}%` }}
                />
              </div>
            </div>
          ))}
        </div>
      )}

      {waiting.hasHeightWarning && (
        <div
          role="alert"
          aria-label="카메라 높이 불일치 경고"
          className="flex flex-col gap-2 rounded-lg bg-delay/15 px-3.5 py-3 text-xs text-delay"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold">
              카메라 높이 불일치 경고 ({waiting.heightWarnings.map((w) => w.cameraId).join(', ')})
            </span>
            {onReturnToStep1 && (
              <Button size="sm" variant="secondary" onClick={onReturnToStep1}>
                1단계 높이 수정
              </Button>
            )}
          </div>
          <ul className="flex flex-col gap-1 font-mono text-[11px]">
            {waiting.heightWarnings.map((w) => (
              <li key={w.cameraId}>{w.message}</li>
            ))}
          </ul>
        </div>
      )}

      {waiting.failed && (
        <div
          role="alert"
          aria-label="세팅 실패 안내"
          className="flex flex-col gap-2 rounded-lg bg-delay/15 px-3.5 py-3 text-xs text-delay"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold">
              세팅 실패{waiting.failureCode ? ` (${waiting.failureCode})` : ''}
            </span>
            {onRetry && (
              <Button size="sm" disabled={retryPending} onClick={onRetry}>
                다시 시도
              </Button>
            )}
          </div>
          <p className="text-foreground">
            {waiting.failureMessage ?? '3D 공간 복원 및 보정에 실패했습니다. 다시 시도해 주세요.'}
          </p>
        </div>
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
    </Panel>
  );
}

/** 미리보기 맵 · 정밀 Scene 상태 배지 및 AI 위험 구역 초안(수락·버리기) 패널. */
export function AiZoneDraftsPanel({
  sceneTier,
  draftsResponse,
  acceptedDraftIds,
  discardedDraftIds,
  pendingDraftId,
  acceptFailure,
  onAcceptDraft,
  onDiscardDraft,
}: {
  sceneTier: SceneTierStatusView;
  draftsResponse: SetupZoneDraftsResponse | null;
  /** 구역으로 만든 초안. */
  acceptedDraftIds: readonly string[];
  discardedDraftIds: readonly string[];
  /** 구역으로 만드는 중인 초안. 그동안 다른 초안은 수락하지 않는다. */
  pendingDraftId: string | null;
  acceptFailure: DraftAcceptFailure | null;
  onAcceptDraft: (item: ZoneDraftItem) => void;
  onDiscardDraft: (candidateId: string) => void;
}) {
  const allDrafts = draftsResponse?.drafts ?? [];
  const visibleDrafts = allDrafts.filter((d) => !discardedDraftIds.includes(d.candidate_id));
  const tierTone: SetupTone =
    sceneTier.tier === 'FINAL' ? 'ok' : sceneTier.tier === 'PREVIEW' ? 'active' : 'idle';
  const activeSceneVersion = sceneTier.finalSceneVersion ?? sceneTier.previewSceneVersion;

  return (
    <Panel title="AI 위험 구역 초안 · 배경 맵 상태" fill={1}>
      <div
        role="status"
        aria-label="배경 맵 단계 상태"
        className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-surface-2 px-3 py-2 text-xs"
      >
        <div className="flex items-center gap-2">
          <Badge
            className={cn(
              'h-auto rounded px-2 py-0.5 font-mono text-xs font-semibold',
              TONE_BADGE[tierTone],
            )}
          >
            {sceneTier.label}
          </Badge>
          <span className="text-muted-foreground">{sceneTier.description}</span>
        </div>
        {activeSceneVersion && (
          <span className="font-mono text-[11px] text-muted-foreground">{activeSceneVersion}</span>
        )}
      </div>

      {draftsResponse?.status === 'QUEUED' || draftsResponse?.status === 'NOT_REQUESTED' ? (
        <p className="text-xs text-muted-foreground">AI 구역 초안 분석 중</p>
      ) : visibleDrafts.length === 0 ? (
        <p className="text-xs text-muted-foreground">AI 구역 초안 없음</p>
      ) : (
        <ul aria-label="AI 구역 초안 목록" className="flex flex-col gap-2.5">
          {visibleDrafts.map((item) => {
            const isAccepted = acceptedDraftIds.includes(item.candidate_id);
            const isNoOpinion = item.no_opinion;
            return (
              <li
                key={item.candidate_id}
                data-candidate-id={item.candidate_id}
                data-no-opinion={isNoOpinion ? 'true' : 'false'}
                className={cn(
                  'flex flex-col gap-2 rounded-lg border border-dashed px-3 py-2.5 text-xs',
                  isNoOpinion
                    ? 'border-muted-foreground/50 bg-surface-3/60 text-muted-foreground'
                    : 'border-primary/40 bg-surface-2 text-foreground',
                )}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <strong className="text-sm font-semibold text-foreground">
                      {item.recommended_name}
                    </strong>
                    <Badge className="h-auto rounded bg-surface-3 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                      {CANDIDATE_KIND_LABEL[item.candidate_kind]}
                    </Badge>
                    {isNoOpinion ? (
                      <Badge
                        aria-label={`${item.recommended_name} AI 의견 없음`}
                        className="h-auto rounded bg-surface-3 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-muted-foreground"
                      >
                        AI 의견 없음
                      </Badge>
                    ) : (
                      <Badge
                        className={cn(
                          'h-auto rounded px-1.5 py-0.5 font-mono text-[11px] font-semibold',
                          item.recommended_severity === 'CRITICAL'
                            ? 'bg-sev-critical/15 text-sev-critical'
                            : 'bg-sev-high/15 text-sev-high',
                        )}
                      >
                        {item.recommended_severity}
                      </Badge>
                    )}
                    {isAccepted && (
                      <Badge className="h-auto rounded bg-primary/15 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-point-soft">
                        반영됨
                      </Badge>
                    )}
                  </div>
                  <span className="font-mono text-[11px] text-muted-foreground">
                    점선 초안 · Z {item.z_min_m.toFixed(1)}~{item.z_max_m.toFixed(1)}m
                    {item.confidence !== null && item.confidence !== undefined
                      ? ` · 신뢰도 ${Math.round(item.confidence * 100)}%`
                      : ''}
                  </span>
                </div>

                <p className="text-xs text-muted-foreground">{item.rationale}</p>

                {acceptFailure?.candidateId === item.candidate_id && (
                  <p role="alert" className="text-xs font-medium text-delay">
                    {acceptFailure.text}
                  </p>
                )}

                {!isAccepted && (
                  <div className="flex items-center justify-end gap-2">
                    <Button
                      size="sm"
                      variant="secondary"
                      aria-label={`${item.recommended_name} 버리기`}
                      disabled={pendingDraftId === item.candidate_id}
                      onClick={() => onDiscardDraft(item.candidate_id)}
                    >
                      버리기
                    </Button>
                    <Button
                      size="sm"
                      aria-label={`${item.recommended_name} 수락`}
                      disabled={pendingDraftId !== null}
                      onClick={() => onAcceptDraft(item)}
                    >
                      수락
                    </Button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
