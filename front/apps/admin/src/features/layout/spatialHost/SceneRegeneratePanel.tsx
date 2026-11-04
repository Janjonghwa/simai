import { useCurrentRole } from '@/features/auth/useCurrentRole';
import {
  parseSceneRegenerateQuality,
  SCENE_REGENERATE_MAX_REASON_LENGTH,
  SCENE_REGENERATE_PREVIOUS_FILL_NOTICE_RATIO,
  SCENE_REGENERATE_STEP_LABELS,
  sceneRegenerateFailureText,
  type SceneRegenerateStatus,
} from '@/features/layout/api';
import { LIVE_WORK_NOTICE, useSceneRegenerate } from '@/features/layout/useSceneRegenerate';
import { CheckCircle2, Loader2, RefreshCw, Sparkles, TriangleAlert } from 'lucide-react';
import { useState } from 'react';

export interface SceneRegeneratePanelProps {
  /** 현재 활성화된 Scene 버전(이미 후보가 활성화된 경우 버튼 상태 구분용). */
  currentSceneVersion?: string | null;
  /** 현재 Scene 조회 응답 본문. `quality` 의 재생성 품질을 읽는다(-469). */
  currentScene?: unknown;
}

const pct = (r: number) => `${Math.round(r * 100)}%`;

const STATUS_BADGE_CLASS: Record<SceneRegenerateStatus, string> = {
  REQUESTED: 'border-sky-500/40 bg-sky-950/80 text-sky-200',
  CAPTURING: 'border-indigo-500/40 bg-indigo-950/80 text-indigo-200',
  RECONSTRUCTING: 'border-amber-500/40 bg-amber-950/80 text-amber-200',
  CANDIDATE_READY: 'border-emerald-500/40 bg-emerald-950/80 text-emerald-200',
  FAILED: 'border-red-500/40 bg-red-950/80 text-red-200',
};

/**
 * 실시간 모니터링 화면의 관리자용 3D 배경(맵) 재생성 도구(S15P31A101-428).
 * 관리자(ADMIN)에게만 보이며 모바일 폭에서는 숨긴다(hidden sm:flex).
 */
export function SceneRegeneratePanel({
  currentSceneVersion,
  currentScene,
}: SceneRegeneratePanelProps) {
  const role = useCurrentRole();
  const isAdmin = role === 'ADMIN';
  const [reason, setReason] = useState('');
  const {
    current,
    isInProgress,
    liveWorkInProgress,
    isSubmitting,
    isActivating,
    activatedSceneVersion,
    requestError,
    regenerate,
    activateCandidate,
  } = useSceneRegenerate(isAdmin);

  if (!isAdmin) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await regenerate(reason);
  };

  const quality = parseSceneRegenerateQuality(currentScene);
  const cameras = quality?.camera_background_quality
    ? Object.entries(quality.camera_background_quality)
    : [];

  const candidateVersion =
    current?.status === 'CANDIDATE_READY' ? (current.scene_version ?? null) : null;
  const isCandidateAlreadyActive =
    candidateVersion !== null &&
    (activatedSceneVersion === candidateVersion || currentSceneVersion === candidateVersion);

  return (
    <div
      data-testid="scene-regenerate-panel"
      aria-label="맵 재생성 도구"
      className="pointer-events-auto absolute top-[72px] left-[136px] z-[5] hidden flex-col gap-1.5 rounded-[12px] bg-card/94 px-3 py-1.5 text-xs text-foreground shadow-float backdrop-blur-[8px] sm:flex"
    >
      <form onSubmit={handleSubmit} className="flex items-center gap-1.5">
        <input
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          maxLength={SCENE_REGENERATE_MAX_REASON_LENGTH}
          disabled={isInProgress || isSubmitting}
          placeholder="재생성 사유 (선택, 120자)"
          aria-label="재생성 사유"
          className="h-7 w-40 rounded border border-border bg-background/80 px-2 text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={isInProgress || isSubmitting || liveWorkInProgress}
          title={liveWorkInProgress ? LIVE_WORK_NOTICE : undefined}
          className="inline-flex h-7 items-center gap-1 rounded border border-border bg-secondary px-2.5 text-xs font-medium text-secondary-foreground transition hover:bg-secondary/80 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isInProgress || isSubmitting ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-400" aria-hidden="true" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5 text-sky-400" aria-hidden="true" />
          )}
          <span>맵 재생성</span>
        </button>
      </form>

      {liveWorkInProgress && (
        <p data-testid="scene-regenerate-live-notice" className="text-[11px] text-amber-400">
          {LIVE_WORK_NOTICE}
        </p>
      )}

      {current && (
        <div className="flex flex-wrap items-center gap-1.5" role="status" aria-live="polite">
          <span
            data-testid="scene-regenerate-status"
            className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] font-medium ${STATUS_BADGE_CLASS[current.status]}`}
          >
            {isInProgress && <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />}
            {SCENE_REGENERATE_STEP_LABELS[current.status]}
          </span>

          {current.status === 'FAILED' && (
            <span
              data-testid="scene-regenerate-failure-reason"
              className="inline-flex items-center gap-1 text-[11px] text-red-400"
            >
              <TriangleAlert className="h-3 w-3 shrink-0" aria-hidden="true" />
              {sceneRegenerateFailureText(current.failure_reason)}
            </span>
          )}

          {candidateVersion && !isCandidateAlreadyActive && (
            <button
              type="button"
              onClick={() => void activateCandidate()}
              disabled={isActivating}
              className="inline-flex items-center gap-1 rounded border border-emerald-500/50 bg-emerald-600 px-2.5 py-0.5 text-[11px] font-semibold text-white transition hover:bg-emerald-500 disabled:opacity-50"
            >
              <Sparkles className="h-3 w-3" aria-hidden="true" />
              <span>새 3D 배경 후보 — 활성화</span>
            </button>
          )}

          {candidateVersion && isCandidateAlreadyActive && (
            <span className="inline-flex items-center gap-1 text-[11px] text-emerald-400">
              <CheckCircle2 className="h-3 w-3" aria-hidden="true" />새 3D 배경 활성화됨 (
              {candidateVersion})
            </span>
          )}
        </div>
      )}

      {quality && (
        <div data-testid="scene-regenerate-quality" className="flex flex-col gap-0.5 text-[11px]">
          <span className="text-muted-foreground">
            이전 모습으로 채움 {pct(quality.previous_fill_ratio)} · 비움{' '}
            {pct(quality.unrevealed_ratio)}
          </span>
          {cameras.length > 0 && (
            <span data-testid="scene-regenerate-quality-cameras" className="text-muted-foreground">
              {cameras
                .map(
                  ([id, q]) =>
                    `${id} 채움 ${pct(q.previous_fill_ratio)}/비움 ${pct(q.unrevealed_ratio)}`,
                )
                .join(' · ')}
            </span>
          )}
          {quality.previous_fill_ratio >= SCENE_REGENERATE_PREVIOUS_FILL_NOTICE_RATIO && (
            <span data-testid="scene-regenerate-quality-notice" className="text-violet-300">
              일부는 이전 모습입니다
            </span>
          )}
        </div>
      )}

      {requestError && (
        <p role="alert" className="text-[11px] text-amber-400">
          {requestError}
        </p>
      )}
    </div>
  );
}
