import { PageTitle } from '@/features/layout/PageTitle';
import {
  SpatialViewportContext,
  useSpatialViewport,
  type SpatialViewportValue,
} from '@/features/layout/SpatialViewportHost';
import { RulesPanel } from '@/features/rules/RulesPanel';
import {
  cancelSetupSession,
  requestWorkUnitSetupSessionId,
  setupSessionKey,
  setupWorkUnitSessionKey,
} from '@/features/setup/api';
import {
  ACTION_LABEL,
  deriveHeightFields,
  deriveStep1Coverage,
  deriveWaitingView,
  MAP_TITLE,
  resolveHeightCameraIds,
  SECONDARY_ACTIONS,
  STEPS,
  validateHeightFields,
  type ActionKey,
  type CalibrationState,
  type SetupTone,
  type Stage,
} from '@/features/setup/setup';
import {
  acceptSetupZoneDraft,
  discardSetupZoneDraft,
  setActiveSetupSessionId,
  setSetupWorkUnitId,
  useSetupFlowStore,
} from '@/features/setup/setup.store';
import { SetupMap } from '@/features/setup/SetupMap';
import {
  AiZoneDraftsPanel,
  CameraHeightPanel,
  CameraPanel,
  SetupCctvPanel,
  StagePanel,
  WaitingPanel,
} from '@/features/setup/SetupPanels';
import {
  useCameraHeightDefaults,
  useCoverageGuides,
  useCreateSetupSession,
  useRetrySetupSession,
  useSetupSession,
  useSetupZoneDrafts,
} from '@/features/setup/useSetupStep1';
import type { CommandResult, ViewerTarget } from '@/features/viewer3d';
import { useZones } from '@/features/zones/useZones';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { api } from '@/lib/api';
import { Alert } from '@simai/ui/components/alert';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { Check } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

// 설치 · 보정(명세 8.1·13.4·18.1, S15P31A101-448·-453):
// 1단계: CCTV 4칸 실시간 미리보기, 카메라 각도 헬퍼(2~3초 폴링), 카메라별 실측 높이 입력.
// 2단계(대기 화면): POST /api/setup-sessions 후 GET /api/setup-sessions/{id} 2초 주기 조회,
// 단계(배경 수집 → 3D 공간·좌표계 → 카메라 정밀 보정 → 미리보기 맵·구역 초안)·진행률·남은 시간(목표 약 1분 30초),
// 실패 시 사유 + 다시 시도(POST /api/setup-sessions/{id}/retry), 높이 불일치 경고 시 1단계 높이 칸 복귀 후 재시도.
// 3단계(미리보기 맵 도착 → 구역 편집): 미리보기 Scene 준비 시 구역 편집 도구(ZonesPage, -456 재사용) 및
// AI 위험 구역 초안(GET /api/setup-sessions/{id}/zone-drafts, 점선 다각형, 수락·버리기, AI 의견 없음 회색 표시),
// 고화질 정식 Scene 도착 시 자동 교체(편집 초안 유지), 구역 발행(POST /api/zones/commit) 뒤 작업 관리(/work?workUnitId=) 복귀.

const STATE_TONE: Record<CalibrationState, string> = {
  UNCALIBRATED: 'bg-surface-3 text-muted-foreground',
  PROVISIONAL: 'bg-primary/15 text-point-soft',
  DRIFTED: 'bg-delay/15 text-delay',
  CALIBRATED: 'bg-ok/15 text-ok',
};

const TONE_BADGE: Record<SetupTone, string> = {
  ok: 'bg-ok/15 text-ok',
  active: 'bg-primary/15 text-point-soft',
  delay: 'bg-delay/15 text-delay',
  idle: 'bg-surface-3 text-muted-foreground',
};

const STAGE_CALIBRATION: readonly { state: CalibrationState; version: string }[] = [
  { state: 'UNCALIBRATED', version: '세팅 1단계 · 카메라 확인' },
  { state: 'PROVISIONAL', version: '' },
  { state: 'CALIBRATED', version: '' },
  { state: 'CALIBRATED', version: '' },
];

function SetupZoneEditorHost({ children }: { children: ReactNode }) {
  const existing = useSpatialViewport();
  const { zones, raw: zonesRaw, failed: zonesFailed } = useZones();
  const [selectedZoneId, setSelectedZoneId] = useState<string | null>(null);

  const selectTarget = useCallback((target: ViewerTarget): CommandResult => {
    if (target.type === 'zone') setSelectedZoneId(target.id);
    return { ok: true };
  }, []);

  const clearSelection = useCallback(() => {
    setSelectedZoneId(null);
  }, []);

  const fallbackValue = useMemo<SpatialViewportValue>(
    () => ({
      view: '2d',
      viewerReady: false,
      selectedKey: selectedZoneId ? `zone:${selectedZoneId}` : null,
      selectedZoneId,
      zones,
      zonesRaw,
      zonesFailed,
      selectTarget,
      clearSelection,
    }),
    [selectedZoneId, zones, zonesRaw, zonesFailed, selectTarget, clearSelection],
  );

  if (existing) return <>{children}</>;
  return <SpatialViewportContext value={fallbackValue}>{children}</SpatialViewportContext>;
}

/** 작업 없이 세팅 화면을 열면 어느 작업의 세팅인지 모르므로 작업 관리로 돌려보낸다(작업 id 를 지어내지 않는다). */
function SetupNeedsWork() {
  const navigate = useNavigate();
  return (
    <>
      <PageTitle>설치 · 보정</PageTitle>
      <Alert role="status" data-testid="setup-needs-work" className="flex flex-col gap-2">
        <p className="text-sm font-semibold">어느 작업의 세팅인지 알 수 없습니다.</p>
        <p className="text-xs text-muted-foreground">
          현장 세팅은 작업마다 합니다. 작업 관리에서 작업을 고르고 &quot;세팅 시작&quot;을 눌러
          들어오세요.
        </p>
        <div>
          <Button size="sm" onClick={() => void navigate({ to: '/work' })}>
            작업 관리로 이동
          </Button>
        </div>
      </Alert>
    </>
  );
}

export function SetupPage({ workUnitId }: { workUnitId?: number } = {}) {
  if (workUnitId === undefined) return <SetupNeedsWork />;
  return <SetupWorkPage workUnitId={workUnitId} />;
}

function SetupWorkPage({ workUnitId }: { workUnitId: number }) {
  useEffect(() => setSetupWorkUnitId(workUnitId), [workUnitId]);
  const [stage, setStage] = useState<Stage>(0);
  const [heightEdits, setHeightEdits] = useState<Record<string, string>>({});
  const [returnForHeightRetry, setReturnForHeightRetry] = useState(false);
  const navigate = useNavigate();

  const activeSessionId = useSetupFlowStore((s) => s.activeSessionId);
  const acceptedDraftIds = useSetupFlowStore((s) => s.acceptedDraftIds);
  const discardedDraftIds = useSetupFlowStore((s) => s.discardedDraftIds);
  const zonesCommitted = useSetupFlowStore((s) => s.zonesCommitted);

  // 새로고침·다른 화면에서 돌아왔을 때: 이 작업에 진행 중인(또는 끝난) 세션이 있으면 이어받아 대기 화면·구역 단계로 연다.
  // 세션 ID 를 메모리에만 두면 다시 시작하다 409 가 나므로 서버의 work_unit.active_setup_session_id 를 정본으로 본다.
  const workUnitSessionQuery = useQuery({
    queryKey: setupWorkUnitSessionKey(workUnitId),
    queryFn: ({ signal }) => requestWorkUnitSetupSessionId(api, workUnitId, signal),
    enabled: activeSessionId === null,
    retry: false,
    staleTime: 0,
  });
  const resumedRef = useRef(false);
  useEffect(() => {
    const resumeId = workUnitSessionQuery.data;
    if (resumedRef.current || activeSessionId !== null || !resumeId) return;
    resumedRef.current = true;
    setActiveSetupSessionId(resumeId);
    setStage(1);
  }, [workUnitSessionQuery.data, activeSessionId]);

  const step1Active = stage === 0;
  const coverageQuery = useCoverageGuides(step1Active);
  const heightDefaultsQuery = useCameraHeightDefaults();

  const go = useCallback((next: Stage) => {
    const clamped = Math.min(next, (STEPS.length - 1) as Stage) as Stage;
    setStage(clamped);
  }, []);

  const createSession = useCreateSetupSession(() => {
    setHeightEdits({});
    setReturnForHeightRetry(false);
    go(1);
  });

  const retrySession = useRetrySetupSession(() => {
    setHeightEdits({});
    setReturnForHeightRetry(false);
    go(1);
  });

  const sessionId =
    activeSessionId ??
    retrySession.session?.setup_session_id ??
    createSession.session?.setup_session_id ??
    null;

  const sessionQuery = useSetupSession(sessionId, Boolean(sessionId));
  const currentSession = sessionQuery.data ?? retrySession.session ?? createSession.session ?? null;

  const waiting = useMemo(() => deriveWaitingView(currentSession), [currentSession]);

  // 워커가 죽거나 작업이 멈춰 같은 현장의 새 세팅이 409 로 막힐 때 관리자가 진행 중 세션을 취소한다.
  const queryClient = useQueryClient();
  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelSetupSession(api, id),
    onSuccess: (session) => {
      queryClient.setQueryData(setupSessionKey(session.setup_session_id), session);
    },
  });
  const sessionRunning =
    currentSession !== null &&
    currentSession.status !== 'COMPLETED' &&
    currentSession.status !== 'FAILED';
  const handleCancelSession = useCallback(() => {
    if (!sessionId || cancelMutation.isPending) return;
    if (!window.confirm('진행 중인 세팅을 취소할까요? 취소하면 처음부터 다시 시작해야 합니다.'))
      return;
    cancelMutation.mutate(sessionId);
  }, [sessionId, cancelMutation]);

  const autoMovedSessionRef = useRef<string | null>(null);
  useEffect(() => {
    if (
      stage === 1 &&
      waiting.previewReady &&
      !waiting.hasHeightWarning &&
      currentSession?.setup_session_id &&
      autoMovedSessionRef.current !== currentSession.setup_session_id
    ) {
      autoMovedSessionRef.current = currentSession.setup_session_id;
      go(2);
    }
  }, [stage, waiting.previewReady, waiting.hasHeightWarning, currentSession?.setup_session_id, go]);

  const draftsEnabled = Boolean(sessionId) && (waiting.previewReady || stage >= 2);
  const zoneDraftsQuery = useSetupZoneDrafts(sessionId, draftsEnabled);

  const cameraIds = useMemo(
    () => resolveHeightCameraIds(heightDefaultsQuery.data, coverageQuery.data),
    [heightDefaultsQuery.data, coverageQuery.data],
  );

  const effectiveHeightDefaults =
    returnForHeightRetry && currentSession?.camera_heights_m
      ? currentSession.camera_heights_m
      : heightDefaultsQuery.data?.camera_heights_m;

  const heightFields = useMemo(
    () => deriveHeightFields(cameraIds, effectiveHeightDefaults, heightEdits),
    [cameraIds, effectiveHeightDefaults, heightEdits],
  );

  const heightValidation = useMemo(() => validateHeightFields(heightFields), [heightFields]);

  const coverage = useMemo(
    () => deriveStep1Coverage(coverageQuery.data, coverageQuery.error, cameraIds),
    [coverageQuery.data, coverageQuery.error, cameraIds],
  );

  const onChangeHeight = useCallback((cameraId: string, value: string) => {
    setHeightEdits((prev) => ({ ...prev, [cameraId]: value }));
  }, []);

  const submitRetryWithHeights = useCallback(() => {
    if (!sessionId || !heightValidation.valid || retrySession.pending) return;
    retrySession.retry({
      setupSessionId: sessionId,
      body: { camera_heights_m: heightValidation.cameraHeightsM },
    });
  }, [sessionId, heightValidation, retrySession]);

  const submitStep1 = useCallback(() => {
    if (!heightValidation.valid) return;
    if (returnForHeightRetry && sessionId) {
      submitRetryWithHeights();
      return;
    }
    if (createSession.pending) return;
    createSession.submit({
      work_unit_id: workUnitId,
      camera_heights_m: heightValidation.cameraHeightsM,
    });
  }, [
    heightValidation,
    returnForHeightRetry,
    sessionId,
    submitRetryWithHeights,
    createSession,
    workUnitId,
  ]);

  const handleRetryFailedSession = useCallback(() => {
    if (!sessionId || retrySession.pending) return;
    retrySession.retry({
      setupSessionId: sessionId,
      body: currentSession?.camera_heights_m
        ? { camera_heights_m: currentSession.camera_heights_m }
        : {},
    });
  }, [sessionId, retrySession, currentSession]);

  const handleReturnToStep1ForHeight = useCallback(() => {
    if (currentSession?.camera_heights_m) {
      const initialEdits: Record<string, string> = {};
      for (const [camId, val] of Object.entries(currentSession.camera_heights_m)) {
        initialEdits[camId] = String(val);
      }
      setHeightEdits(initialEdits);
    }
    setReturnForHeightRetry(true);
    setStage(0);
  }, [currentSession]);

  const handleNavigateTbm = useCallback(() => {
    void navigate({ to: '/work', search: { workUnitId: String(workUnitId) } });
  }, [navigate, workUnitId]);

  const stageActions: ActionKey[] =
    stage === 0 ? [] : stage === 1 ? ['confirm'] : ['restart', 'zones'];
  const stageMeta = STAGE_CALIBRATION[stage] ?? STAGE_CALIBRATION[0]!;

  const run: Record<ActionKey | 'rewalk', () => void> = {
    start: submitStep1,
    rewalk: () => go(1),
    confirm: () => go(3),
    restart: () => go(0),
    zones: () => void navigate({ to: '/', search: { edit: true } }),
  };

  const step1Pending = createSession.pending || retrySession.pending;
  const step1Error = retrySession.error ?? createSession.error;
  const draftsList = zoneDraftsQuery.data?.drafts ?? [];

  return (
    <>
      <PageTitle>설치 · 보정</PageTitle>
      <div className="flex flex-wrap items-center gap-3">
        {stage === 0 ? (
          <>
            <Badge
              aria-label={`카메라 연결 상태 ${coverage.connectivity}`}
              className={cn(
                'h-auto rounded-md px-2.5 py-0.5 font-mono text-[13px] font-semibold',
                TONE_BADGE[coverage.connectivityTone],
              )}
            >
              {coverage.connectivity} · {coverage.connectivityLabel}
            </Badge>
            {coverage.stale && (
              <Badge
                aria-label="각도 안내 오래된 결과 STALE"
                className="h-auto rounded-md bg-delay/15 px-2.5 py-0.5 font-mono text-[13px] font-semibold text-delay"
              >
                STALE · 오래된 결과
              </Badge>
            )}
            {coverage.calculationFailed && (
              <Badge
                aria-label="각도 안내 계산 실패"
                className="h-auto rounded-md bg-surface-3 px-2.5 py-0.5 text-[13px] font-semibold text-muted-foreground"
              >
                계산 실패
              </Badge>
            )}
          </>
        ) : (
          <>
            <Badge
              className={cn(
                'h-auto rounded-md px-2.5 py-0.5 font-mono text-[13px] font-semibold',
                STATE_TONE[stageMeta.state],
              )}
            >
              {stageMeta.state}
            </Badge>
            {waiting.sceneTier.tier !== 'NONE' && (
              <Badge
                aria-label={`맵 단계 ${waiting.sceneTier.label}`}
                className={cn(
                  'h-auto rounded-md px-2.5 py-0.5 font-mono text-[13px] font-semibold',
                  waiting.sceneTier.tier === 'FINAL' ? TONE_BADGE.ok : TONE_BADGE.active,
                )}
              >
                {waiting.sceneTier.label}
              </Badge>
            )}
          </>
        )}
        <span className="font-mono text-muted-foreground">
          {currentSession?.setup_session_id
            ? `세션 ${currentSession.setup_session_id}${
                currentSession.calibration_version ? ` · ${currentSession.calibration_version}` : ''
              }`
            : stageMeta.version}
        </span>
        <div className="ml-auto flex flex-wrap gap-2">
          {stage === 0 ? (
            <Button
              size="lg"
              className="px-4"
              disabled={!heightValidation.valid || step1Pending}
              onClick={run.start}
            >
              {coverage.submitLabel}
            </Button>
          ) : (
            <>
              {stageActions.map((key) => {
                const secondary = SECONDARY_ACTIONS.has(key);
                return (
                  <Button
                    key={key}
                    size="lg"
                    variant={secondary ? 'secondary' : 'default'}
                    className={cn('px-4', secondary && 'bg-surface-3 hover:bg-surface-3/70')}
                    onClick={run[key]}
                  >
                    {ACTION_LABEL[key]}
                  </Button>
                );
              })}
              {stage === 1 && sessionRunning && (
                <Button
                  size="lg"
                  variant="secondary"
                  className="bg-surface-3 px-4 hover:bg-surface-3/70"
                  disabled={cancelMutation.isPending}
                  onClick={handleCancelSession}
                >
                  세팅 취소
                </Button>
              )}
              {zonesCommitted && (
                <Button size="lg" className="px-4" onClick={handleNavigateTbm}>
                  작업 관리(TBM)로 이동
                </Button>
              )}
            </>
          )}
        </div>
      </div>

      <ol aria-label="보정 단계" className="grid grid-cols-3 gap-2">
        {STEPS.map((label, k) => {
          const done = k < stage;
          const cur = k === stage;
          return (
            <li key={label}>
              <Button
                variant="ghost"
                aria-current={cur ? 'step' : undefined}
                onClick={() => go(k as Stage)}
                className={cn(
                  'h-auto w-full justify-start gap-2.5 rounded-lg bg-card px-3.5 py-2.5 text-left font-normal text-muted-foreground',
                  cur && 'font-semibold text-foreground ring-[1.5px] ring-primary ring-inset',
                )}
              >
                <span
                  className={cn(
                    'grid size-[22px] flex-none place-items-center rounded-full font-mono text-xs',
                    done || cur
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-surface-3 text-muted-foreground',
                  )}
                >
                  {done ? <Check aria-hidden className="size-3.5" /> : k + 1}
                </span>
                {label}
              </Button>
            </li>
          );
        })}
      </ol>

      {stage === 0 && (
        <Alert
          role="status"
          aria-label="카메라 방향 안내"
          className={cn(
            'flex flex-wrap items-center justify-between gap-2 rounded-[10px] border-0 px-4 py-2.5 text-sm',
            coverage.stale || !coverage.canProceedCleanly
              ? 'bg-delay/15 text-delay'
              : 'bg-primary/15 text-point-soft',
          )}
        >
          <span>{coverage.directionLine}</span>
          {coverage.statusNotices.length > 0 && (
            <span className="font-mono text-xs">{coverage.statusNotices.join(' · ')}</span>
          )}
        </Alert>
      )}

      {stage === 0 ? (
        <div className="grid grid-cols-[minmax(0,1.25fr)_minmax(320px,1fr)] items-start gap-3">
          <div className="flex flex-col gap-3">
            <SetupCctvPanel />
            <Card size="sm">
              <CardHeader>
                <CardTitle>
                  <h2>{MAP_TITLE[0]}</h2>
                </CardTitle>
              </CardHeader>
              <CardContent>
                <SetupMap stage={0} coverage={coverage} />
              </CardContent>
            </Card>
          </div>
          <div className="flex flex-col gap-3">
            <CameraPanel coverage={coverage} />
            <CameraHeightPanel
              fields={heightFields}
              onChange={onChangeHeight}
              submitError={step1Error}
              heightWarnings={waiting.heightWarnings}
              retrySessionId={returnForHeightRetry ? sessionId : null}
              onRetryWithHeights={submitRetryWithHeights}
              retryPending={retrySession.pending}
              canSubmitHeights={heightValidation.valid}
            />
          </div>
        </div>
      ) : stage === 1 ? (
        <div className="grid grid-cols-[minmax(0,1.25fr)_minmax(320px,1fr)] items-start gap-3">
          <Card size="sm">
            <CardHeader>
              <CardTitle>
                <h2>{MAP_TITLE[stage]}</h2>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <SetupMap
                stage={stage}
                coverage={coverage}
                drafts={draftsList}
                acceptedDraftIds={acceptedDraftIds}
                discardedDraftIds={discardedDraftIds}
              />
            </CardContent>
          </Card>
          <div className="flex flex-col gap-3">
            {currentSession ? (
              <WaitingPanel
                waiting={waiting}
                onRetry={handleRetryFailedSession}
                retryPending={retrySession.pending}
                retryError={retrySession.error}
                onReturnToStep1={handleReturnToStep1ForHeight}
              />
            ) : (
              <>
                <CameraPanel coverage={coverage} />
                <StagePanel stage={stage} cameraCount={cameraIds.length} />
              </>
            )}
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-[minmax(0,1.25fr)_minmax(320px,1fr)] items-start gap-3">
          <div className="flex flex-col gap-3">
            <Card size="sm">
              <CardHeader>
                <CardTitle>
                  <h2>
                    {waiting.sceneTier.tier === 'FINAL'
                      ? `정밀 3D 맵 · ${waiting.sceneTier.finalSceneVersion ?? 'cal-8'}`
                      : waiting.sceneTier.tier === 'PREVIEW'
                        ? `미리보기 맵 · ${waiting.sceneTier.previewSceneVersion ?? 'cal-8'}`
                        : MAP_TITLE[stage]}
                  </h2>
                </CardTitle>
              </CardHeader>
              <CardContent>
                <SetupMap
                  stage={stage}
                  coverage={coverage}
                  drafts={draftsList}
                  acceptedDraftIds={acceptedDraftIds}
                  discardedDraftIds={discardedDraftIds}
                />
              </CardContent>
            </Card>
            <AiZoneDraftsPanel
              sceneTier={waiting.sceneTier}
              draftsResponse={zoneDraftsQuery.data}
              acceptedDraftIds={acceptedDraftIds}
              discardedDraftIds={discardedDraftIds}
              zonesCommitted={zonesCommitted}
              onAcceptDraft={acceptSetupZoneDraft}
              onDiscardDraft={discardSetupZoneDraft}
              onNavigateTbm={handleNavigateTbm}
            />
          </div>
          <div className="flex flex-col gap-3">
            {currentSession && (
              <SetupZoneEditorHost>
                <div className="relative min-h-[420px]">
                  <ZonesPage />
                </div>
              </SetupZoneEditorHost>
            )}
            <StagePanel stage={stage} cameraCount={cameraIds.length} />
          </div>
        </div>
      )}

      <RulesPanel />
    </>
  );
}
