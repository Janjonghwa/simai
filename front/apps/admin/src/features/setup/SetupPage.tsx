import { PageTitle } from '@/features/layout/PageTitle';
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
  draftGhostsOf,
  resolveHeightCameraIds,
  SECONDARY_ACTIONS,
  STEPS,
  validateHeightFields,
  ZONE_STAGE,
  zoneMapTitle,
  type ActionKey,
  type SetupTone,
  type Stage,
} from '@/features/setup/setup';
import {
  discardSetupZoneDraft,
  setActiveSetupSessionId,
  useSetupFlowStore,
} from '@/features/setup/setup.store';
import { SetupMap } from '@/features/setup/SetupMap';
import {
  AiZoneDraftsPanel,
  CameraHeightPanel,
  CameraMapPanel,
  CameraPanel,
  WaitingPanel,
} from '@/features/setup/SetupPanels';
import { SetupZoneStep } from '@/features/setup/SetupZoneStep';
import type { ZoneDraftItem } from '@/features/setup/types';
import { useAcceptZoneDraft } from '@/features/setup/useAcceptZoneDraft';
import {
  submitErrorText,
  useCameraHeightDefaults,
  useCoverageGuides,
  useCreateSetupSession,
  useRetrySetupSession,
  useSetupSession,
  useSetupZoneDrafts,
} from '@/features/setup/useSetupStep1';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { api } from '@/lib/api';
import { Alert } from '@simai/ui/components/alert';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { Check } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

// 설치 · 보정(명세 8.1·13.4·18.1, S15P31A101-448·-453):
// 1단계: CCTV 4칸 실시간 미리보기, 카메라 각도 헬퍼(2~3초 폴링), 카메라별 실측 높이 입력. 세션이 생기기 전에는 2·3단계로 갈 수 없다.
// 2단계(대기 화면): POST /api/setup-sessions 후 GET /api/setup-sessions/{id} 2초 주기 조회,
// 단계(배경 수집 → 3D 공간·좌표계 → 카메라 정밀 보정 → 미리보기 맵·구역 초안)·진행률·남은 시간(목표 약 1분 30초),
// 실패 시 사유 + 다시 시도(POST /api/setup-sessions/{id}/retry), 높이 불일치 경고 시 1단계 높이 칸 복귀 후 재시도.
// 3단계(구역): 미리보기 Scene 이 준비되면 넘어가 왼쪽 큰 3D(SetupZoneStep)에서 구역 상자를 손잡이로 고치고, 오른쪽에
// 구역 편집 도구(ZonesPage) 및 AI 위험 구역 초안(GET /api/setup-sessions/{id}/zone-drafts, 3D 점선 윤곽, 수락하면 바로 구역 저장·버리기)을 둔다.
// 고화질 정식 Scene 도착 시 자동 교체(편집 초안 유지), 구역 발행(POST /api/zones/commit) 뒤 작업 관리(/work?workUnitId=) 복귀.

const TONE_BADGE: Record<SetupTone, string> = {
  ok: 'bg-ok/15 text-ok',
  active: 'bg-primary/15 text-point-soft',
  delay: 'bg-delay/15 text-delay',
  idle: 'bg-surface-3 text-muted-foreground',
};

/** 세션이 아직 없는 1단계 머리에 보이는 글자. */
const STEP1_LABEL = '세팅 1단계 · 카메라 확인';

const STAGE_ACTIONS: Record<Stage, readonly ActionKey[]> = {
  0: [],
  1: ['confirm'],
  2: ['restart'],
};

const NO_DRAFTS: readonly ZoneDraftItem[] = [];

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
  // 이어 온 세팅 세션이 있으면 대기 화면에서 시작한다. 맵이 이미 준비됐으면 곧바로 구역 단계로 넘어간다(작업 관리의 구역
  // 단계도 이 화면을 연다).
  const [stage, setStage] = useState<Stage>(() =>
    useSetupFlowStore.getState().activeSessionId === null ? 0 : 1,
  );
  const [heightEdits, setHeightEdits] = useState<Record<string, string>>({});
  const [returnForHeightRetry, setReturnForHeightRetry] = useState(false);
  const navigate = useNavigate();

  const activeSessionId = useSetupFlowStore((s) => s.activeSessionId);
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

  const go = useCallback((next: Stage) => setStage(next), []);

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

  // 이 브라우저에 남은 세션이 다른 작업의 것이면 버리고 이 작업의 세션을 서버에서 다시 찾는다(위 이어받기). 찾기 전에는
  // 1단계에서 기다린다.
  const foreignSession = currentSession !== null && currentSession.work_unit_id !== workUnitId;
  if (foreignSession && stage !== 0) setStage(0);
  useEffect(() => {
    if (foreignSession) setActiveSetupSessionId(null);
  }, [foreignSession]);

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

  // 이 세션의 구역을 발행했다(이 브라우저에서 방금, 또는 서버 기록).
  const zonesPublished = zonesCommitted || currentSession?.zones_committed_at != null;

  // 높이 경고가 있으면 보정 검증에 머물러 확인을 받는다. 이미 구역을 발행한 세션은 그 확인을 지났으므로 구역 단계로 간다.
  const calibrationAccepted = !waiting.hasHeightWarning || zonesPublished;
  const autoMovedSessionRef = useRef<string | null>(null);
  useEffect(() => {
    if (
      stage === 1 &&
      waiting.previewReady &&
      calibrationAccepted &&
      currentSession?.setup_session_id &&
      autoMovedSessionRef.current !== currentSession.setup_session_id
    ) {
      autoMovedSessionRef.current = currentSession.setup_session_id;
      go(ZONE_STAGE);
    }
  }, [stage, waiting.previewReady, calibrationAccepted, currentSession?.setup_session_id, go]);

  const draftsEnabled = Boolean(sessionId) && (waiting.previewReady || stage === ZONE_STAGE);
  const zoneDraftsQuery = useSetupZoneDrafts(sessionId, draftsEnabled);
  const draftsList = zoneDraftsQuery.data?.drafts ?? NO_DRAFTS;
  const draftAccept = useAcceptZoneDraft(draftsList);
  const acceptedDraftIds = draftAccept.acceptedIds;
  const draftGhosts = useMemo(
    () => draftGhostsOf(draftsList, acceptedDraftIds, discardedDraftIds),
    [draftsList, acceptedDraftIds, discardedDraftIds],
  );

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

  const restartPending = createSession.pending || retrySession.pending || cancelMutation.isPending;

  /**
   * 높이를 고쳐 세팅을 다시 시작한다. 서버 규칙(SetupService)에 맞춰 세션 상태로 가른다: 서버 retry 는 실패한 세션만 받고
   * 끝난 세션은 새로 시작하며, 진행 중인 세션이 있으면 새로 시작할 수 없어 먼저 취소한 뒤 재시도한다.
   */
  const restartWithHeights = useCallback(() => {
    if (!sessionId || !currentSession || !heightValidation.valid || restartPending) return;
    const cameraHeights = heightValidation.cameraHeightsM;
    const retry = () =>
      retrySession.retry({ setupSessionId: sessionId, body: { camera_heights_m: cameraHeights } });
    if (currentSession.status === 'FAILED') retry();
    else if (currentSession.status === 'COMPLETED')
      createSession.submit({ work_unit_id: workUnitId, camera_heights_m: cameraHeights });
    else cancelMutation.mutate(sessionId, { onSuccess: retry });
  }, [
    sessionId,
    currentSession,
    heightValidation,
    restartPending,
    retrySession,
    createSession,
    cancelMutation,
    workUnitId,
  ]);

  const submitStep1 = useCallback(() => {
    if (!heightValidation.valid) return;
    if (returnForHeightRetry && sessionId) {
      restartWithHeights();
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
    restartWithHeights,
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

  const stageActions = STAGE_ACTIONS[stage];

  const run: Record<ActionKey, () => void> = {
    start: submitStep1,
    confirm: () => go(ZONE_STAGE),
    restart: () => go(0),
  };

  const step1Error =
    retrySession.error ?? createSession.error ?? submitErrorText(cancelMutation.error);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
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
            : stage === 0
              ? STEP1_LABEL
              : ''}
        </span>
        <div className="ml-auto flex flex-wrap gap-2">
          {stage === 0 ? (
            <Button
              size="lg"
              className="px-4"
              disabled={!heightValidation.valid || restartPending}
              onClick={run.start}
            >
              {coverage.submitLabel}
            </Button>
          ) : (
            <>
              {/* 보조 단추를 먼저, 다음 단계로 가는 단추를 맨 오른쪽에 둔다(단계가 바뀌어도 자리가 같다). */}
              {stageActions
                .filter((key) => SECONDARY_ACTIONS.has(key))
                .map((key) => (
                  <Button
                    key={key}
                    size="lg"
                    variant="secondary"
                    className="bg-surface-3 px-4 hover:bg-surface-3/70"
                    onClick={run[key]}
                  >
                    {ACTION_LABEL[key]}
                  </Button>
                ))}
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
              {stageActions
                .filter((key) => !SECONDARY_ACTIONS.has(key))
                .map((key) => (
                  <Button key={key} size="lg" className="px-4" onClick={run[key]}>
                    {ACTION_LABEL[key]}
                  </Button>
                ))}
              {stage === ZONE_STAGE && zonesPublished && (
                <Button size="lg" className="px-4" onClick={handleNavigateTbm}>
                  작업 관리로
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
                disabled={k > 0 && !currentSession}
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

      {stage === 0 ? (
        <div className="grid flex-1 grid-cols-[minmax(0,1fr)_minmax(320px,420px)] gap-3">
          <CameraMapPanel coverage={coverage} />
          <div className="flex flex-col gap-3">
            <CameraPanel coverage={coverage} />
            <CameraHeightPanel
              fields={heightFields}
              onChange={onChangeHeight}
              submitError={step1Error}
              heightWarnings={waiting.heightWarnings}
              retrySessionId={returnForHeightRetry ? sessionId : null}
              onRetryWithHeights={restartWithHeights}
              retryPending={restartPending}
              canSubmitHeights={heightValidation.valid}
            />
          </div>
        </div>
      ) : stage === 1 ? (
        <div className="mx-auto flex min-h-0 w-full max-w-[960px] flex-1 flex-col">
          {currentSession && (
            <WaitingPanel
              waiting={waiting}
              onRetry={handleRetryFailedSession}
              retryPending={retrySession.pending}
              retryError={retrySession.error}
              onReturnToStep1={handleReturnToStep1ForHeight}
            />
          )}
        </div>
      ) : (
        <SetupZoneStep
          title={zoneMapTitle(waiting.sceneTier)}
          draftGhosts={draftGhosts}
          plan={
            <SetupMap
              stage={stage}
              coverage={coverage}
              drafts={draftsList}
              acceptedDraftIds={acceptedDraftIds}
              discardedDraftIds={discardedDraftIds}
            />
          }
        >
          {currentSession && <ZonesPage placement="inline" />}
          <AiZoneDraftsPanel
            sceneTier={waiting.sceneTier}
            draftsResponse={zoneDraftsQuery.data}
            acceptedDraftIds={acceptedDraftIds}
            discardedDraftIds={discardedDraftIds}
            pendingDraftId={draftAccept.pendingId}
            acceptFailure={draftAccept.failure}
            onAcceptDraft={(item) => void draftAccept.accept(item)}
            onDiscardDraft={discardSetupZoneDraft}
          />
        </SetupZoneStep>
      )}
    </div>
  );
}
