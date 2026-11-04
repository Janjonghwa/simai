import { tileLabel } from '@/features/cctv/cctv';
import { useCctvStore } from '@/features/cctv/cctv.store';
import { CctvDemo } from '@/features/cctv/CctvDemo';
import { CctvWall, type WallTile } from '@/features/cctv/CctvWall';
import { DIMMED, TILE_FRAME, tileZoom } from '@/features/cctv/tileStyle';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import {
  CAMERA_HEIGHT_HELP,
  CANDIDATE_KIND_LABEL,
  CHECKS,
  HEAD_ERR_MAX_CM,
  MAX_CAMERA_HEIGHT_M,
  PAIR_MISMATCH,
  RESULT,
  type HeightFieldView,
  type HeightWarningItem,
  type SceneTierStatusView,
  type SetupTone,
  type SetupWaitingView,
  type Stage,
  type Step1CoverageView,
} from '@/features/setup/setup';
import type { SetupZoneDraftsResponse, ZoneDraftItem } from '@/features/setup/types';
import { DEFAULT_CCTV_PREVIEW_URL } from '@/lib/parseEnv';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { useCallback, type ReactNode } from 'react';

// 세팅 화면 패널: 1단계 CCTV 4분할(기존 디코딩 재사용)·카메라 연결/방향 안내·카메라 높이 입력, 이후 단계 요약.
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

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>{title}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">{children}</CardContent>
    </Card>
  );
}

function Rows({ children }: { children: ReactNode }) {
  return <ul className="divide-y divide-border">{children}</ul>;
}

function Row({ children }: { children: ReactNode }) {
  return <li className="flex items-center gap-2.5 px-0.5 py-2 text-sm">{children}</li>;
}

interface Stat {
  label: string;
  value: string | null;
  ok?: boolean;
}

function Stats({ items }: { items: Stat[] }) {
  return (
    <dl className="grid grid-cols-2 gap-2.5">
      {items.map((item) => (
        <div key={item.label} className="grid gap-0.5 rounded-lg bg-surface-2 px-3 py-2.5">
          <dt className="text-xs text-muted-foreground">{item.label}</dt>
          <dd className={cn('font-mono text-[17px] font-semibold', item.ok && 'text-ok')}>
            {item.value ?? (
              <>
                <span aria-hidden>—</span>
                <span className="sr-only">값 없음</span>
              </>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function LiveSetupTile({
  cameraId,
  attach,
  wall,
}: {
  cameraId: string;
  attach: (cameraId: string, canvas: HTMLCanvasElement | null) => void;
  wall: WallTile;
}) {
  const phase = useCctvStore((s) => s.views[cameraId]?.phase);
  const link = useCctvStore((s) => s.link);
  const playing = link === 'open' && phase === 'playing';
  const ref = useCallback(
    (canvas: HTMLCanvasElement | null) => attach(cameraId, canvas),
    [attach, cameraId],
  );
  return (
    <button
      type="button"
      onClick={wall.toggle}
      aria-label={`${cameraId} ${wall.focused ? '작게 보기' : '크게 보기'}`}
      className={cn(TILE_FRAME, 'bg-surface-2', tileZoom(wall.focused))}
    >
      <canvas
        ref={ref}
        aria-label={`${cameraId} 영상`}
        className={cn('size-full object-contain', !playing && DIMMED)}
      />
      <span className={cn(CAPTION, 'top-2 left-2')}>
        <span className="font-mono font-semibold">{cameraId}</span>
        <span>{tileLabel(phase, link)}</span>
      </span>
    </button>
  );
}

/** 1단계 실시간 CCTV 4칸(기존 CCTV WebCodecs 디코딩·모의 화면 재사용). */
export function SetupCctvPanel() {
  const { localUrl, stompUrl } = readPreviewUrls();
  const { attach } = useCctvPreview(localUrl, stompUrl);
  const link = useCctvStore((s) => s.link);
  const cameras = useCctvStore((s) => s.cameras);

  return (
    <Panel title="실시간 CCTV 4칸">
      <div className="flex h-[clamp(260px,40vh,520px)] flex-col">
        {link === 'open' && cameras.length > 0 ? (
          <CctvWall
            ids={cameras}
            tile={(id, wall) => <LiveSetupTile cameraId={id} attach={attach} wall={wall} />}
          />
        ) : (
          <div className="flex min-h-0 flex-1 flex-col gap-2">
            <p className="text-xs text-muted-foreground">{tileLabel(undefined, link)}</p>
            <CctvDemo />
          </div>
        )}
      </div>
    </Panel>
  );
}

/** 1단계 카메라별 수신 상태·회전 방향 안내 및 6쌍 매칭 등급. */
export function CameraPanel({ coverage }: { coverage: Step1CoverageView }) {
  return (
    <Panel title="카메라 연결 · 방향 안내">
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

      <div className="grid grid-cols-2 gap-2" role="list" aria-label="카메라 쌍 매칭 등급">
        {coverage.pairs.map((pair) => (
          <div
            key={pair.key}
            role="listitem"
            className="flex items-center justify-between gap-2 rounded-lg bg-surface-2 px-2.5 py-1.5 text-xs"
          >
            <span className="font-mono font-medium">{pair.key}</span>
            <span className="flex items-center gap-1.5">
              {pair.inliers !== null && (
                <span className="font-mono text-muted-foreground">{pair.inliers}점</span>
              )}
              <Badge
                className={cn(
                  'h-auto rounded px-1.5 py-0.5 font-mono text-xs font-semibold',
                  TONE_BADGE[pair.tone],
                )}
              >
                {pair.gradeLabel}
              </Badge>
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

/** 1단계 카메라별 실측 높이(바닥~렌즈 중심, m) 입력 패널. */
export function CameraHeightPanel({
  fields,
  onChange,
  submitError,
  heightWarnings = [],
  retrySessionId = null,
  onRetryWithHeights,
  retryPending = false,
  canSubmitHeights = true,
}: {
  fields: readonly HeightFieldView[];
  onChange: (cameraId: string, value: string) => void;
  submitError: string | null;
  heightWarnings?: readonly HeightWarningItem[];
  retrySessionId?: string | null;
  onRetryWithHeights?: () => void;
  retryPending?: boolean;
  canSubmitHeights?: boolean;
}) {
  const warnedCamIds = new Set(heightWarnings.map((w) => w.cameraId));
  return (
    <Panel title="카메라 높이 입력 (바닥~렌즈 중심, m)">
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
      <div className="grid grid-cols-2 gap-2.5">
        {fields.map((field) => {
          const inputId = `camera-height-${field.cameraId}`;
          const needsInput = field.required || field.missing;
          const isWarned = warnedCamIds.has(field.cameraId);
          return (
            <div
              key={field.cameraId}
              className={cn(
                'flex flex-col gap-1 rounded-lg bg-surface-2 px-3 py-2',
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
                  'h-8 w-full rounded-md border bg-card px-2.5 font-mono text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring',
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
        <p role="alert" className="text-xs font-medium text-delay">
          {submitError}
        </p>
      )}
    </Panel>
  );
}

/** 2단계 대기 화면 패널(배경 수집 → 3D 공간·좌표계 → 카메라 정밀 보정 → 미리보기 맵·구역 초안, 진행률·남은 시간·실패/높이 경고). */
export function WaitingPanel({
  waiting,
  onRetry,
  retryPending = false,
  retryError = null,
  onReturnToStep1,
}: {
  waiting: SetupWaitingView;
  onRetry?: () => void;
  retryPending?: boolean;
  retryError?: string | null;
  onReturnToStep1?: () => void;
}) {
  return (
    <Panel title="현장 자동 보정 · 3D 공간 생성 대기">
      <div className="flex flex-col gap-2 rounded-lg bg-surface-2 px-3.5 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="font-semibold text-foreground">{waiting.stageLabel}</span>
          <span className="font-mono text-muted-foreground" aria-label="세팅 진행률 및 남은 시간">
            진행률 <strong className="text-foreground">{waiting.progressPct}%</strong> · 남은 시간{' '}
            <strong className="text-foreground">{waiting.remainingLabel}</strong> (
            {waiting.remainingClock})
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
          목표 약 1분 30초 · 2초 주기로 진행 상태를 자동 갱신합니다
        </p>
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
        <div className="grid grid-cols-4 gap-2" aria-label="카메라별 배경 수집 진행률">
          {waiting.backgroundCollection.map((cam) => (
            <div
              key={cam.cameraId}
              className="flex items-center justify-between rounded-md bg-surface-2 px-2.5 py-1.5 font-mono text-xs"
            >
              <span>{cam.cameraId}</span>
              <span className="font-semibold text-foreground">{cam.pct}%</span>
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
        <p role="alert" className="text-xs font-medium text-delay">
          {retryError}
        </p>
      )}
    </Panel>
  );
}

/** 미리보기 맵 · 정밀 Scene 상태 배지 및 AI 위험 구역 초안(수락·버리기·발행 뒤 TBM 이동) 패널. */
export function AiZoneDraftsPanel({
  sceneTier,
  draftsResponse,
  acceptedDraftIds,
  discardedDraftIds,
  zonesCommitted,
  onAcceptDraft,
  onDiscardDraft,
  onNavigateTbm,
}: {
  sceneTier: SceneTierStatusView;
  draftsResponse: SetupZoneDraftsResponse | null;
  acceptedDraftIds: readonly string[];
  discardedDraftIds: readonly string[];
  zonesCommitted: boolean;
  onAcceptDraft: (item: ZoneDraftItem) => void;
  onDiscardDraft: (candidateId: string) => void;
  onNavigateTbm: () => void;
}) {
  const allDrafts = draftsResponse?.drafts ?? [];
  const visibleDrafts = allDrafts.filter((d) => !discardedDraftIds.includes(d.candidate_id));
  const tierTone: SetupTone =
    sceneTier.tier === 'FINAL' ? 'ok' : sceneTier.tier === 'PREVIEW' ? 'active' : 'idle';
  const activeSceneVersion = sceneTier.finalSceneVersion ?? sceneTier.previewSceneVersion;

  return (
    <Panel title="AI 위험 구역 초안 · 배경 맵 상태">
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
        <p className="text-xs text-muted-foreground">
          AI 위험 구역 초안을 분석하고 있습니다 · 준비되는 대로 점선 다각형으로 표시됩니다
        </p>
      ) : visibleDrafts.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          표시할 AI 구역 초안이 없습니다. 구역 편집 도구에서 직접 구역을 추가하거나 수정하세요.
        </p>
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
                        초안 반영됨 (발행 전)
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

                <div className="flex items-center justify-end gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    aria-label={`${item.recommended_name} 버리기`}
                    onClick={() => onDiscardDraft(item.candidate_id)}
                  >
                    버리기
                  </Button>
                  <Button
                    size="sm"
                    aria-label={`${item.recommended_name} 수락`}
                    onClick={() => onAcceptDraft(item)}
                  >
                    {isAccepted ? '다시 수락' : '수락'}
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {zonesCommitted && (
        <div
          role="status"
          aria-label="구역 발행 완료 안내"
          className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-ok/15 px-3.5 py-2.5 text-xs text-ok"
        >
          <span className="font-semibold">
            구역 발행이 완료되었습니다. 작업 전 안전점검(TBM)으로 이동해 세션을 시작하세요.
          </span>
          <Button size="sm" onClick={onNavigateTbm}>
            작업 관리(TBM)로 이동
          </Button>
        </div>
      )}
    </Panel>
  );
}

function CheckPanel() {
  const maxErrorCm = Math.max(...CHECKS.map((c) => c.errorCm));
  return (
    <Panel title={`보정 검증 · 머리 높이 오차 ${HEAD_ERR_MAX_CM}cm 이하`}>
      <Rows>
        {CHECKS.map((check, n) => (
          <Row key={n}>
            <Dot tone="ok" />
            <span>{n + 1}번 지점</span>
            <span className="ml-auto font-mono">{`${check.errorCm}cm`}</span>
          </Row>
        ))}
      </Rows>
      <Stats
        items={[
          { label: '최대 오차', value: `${maxErrorCm}cm`, ok: true },
          { label: '카메라 쌍 불일치', value: PAIR_MISMATCH },
        ]}
      />
    </Panel>
  );
}

function ResultPanel({ cameraCount }: { cameraCount: number }) {
  const maxErrorCm = Math.max(...CHECKS.map((c) => c.errorCm));
  return (
    <Panel title="보정 결과">
      <Stats
        items={[
          { label: '재투영 중앙 · p95', value: RESULT.reprojection },
          { label: '머리 높이 오차', value: `최대 ${maxErrorCm}cm`, ok: true },
          { label: '바닥 평면 차이', value: RESULT.floorDiff },
          { label: '스케일 근거', value: `카메라 높이 ${cameraCount}대` },
          { label: '검증 지점', value: `${CHECKS.length} / ${CHECKS.length}` },
        ]}
      />
    </Panel>
  );
}

export function StagePanel({ stage, cameraCount }: { stage: Stage; cameraCount: number }) {
  if (stage === 1) return <CheckPanel />;
  return <ResultPanel cameraCount={cameraCount} />;
}
