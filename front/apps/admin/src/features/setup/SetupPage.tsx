import { PageTitle } from '@/features/layout/PageTitle';
import {
  cancelSetupSession,
  requestWorkUnitSetupSessionId,
  setupSessionKey,
  setupWorkUnitSessionKey,
  setupZonePlanKey,
} from '@/features/setup/api';
import { isSetupSessionRunning } from '@/features/setup/blockingSetup';
import { BlockingSetupNotice } from '@/features/setup/BlockingSetupNotice';
import {
  cameraCheckBlockReason,
  cameraCheckFailed,
  deriveCameraCheckView,
} from '@/features/setup/cameraCheck';
import { CameraRiskDialog } from '@/features/setup/CameraRiskDialog';
import { ModelPickerDialog } from '@/features/setup/ModelPickerDialog';
import {
  deriveHeightFields,
  deriveStep1Coverage,
  deriveWaitingView,
  draftGhostsOf,
  resolveHeightCameraIds,
  sessionSceneVersion,
  STEPS,
  validateHeightFields,
  ZONE_STAGE,
  zoneMapTitle,
  type Stage,
} from '@/features/setup/setup';
import {
  discardSetupZoneDraft,
  setActiveSetupSessionId,
  useSetupFlowStore,
} from '@/features/setup/setup.store';
import { deriveSetupActions, toWorkAction } from '@/features/setup/setupActions';
import { SetupCalibrateStep } from '@/features/setup/SetupCalibrateStep';
import {
  canCompleteSetup,
  SetupCompleteCard,
  setupCompleted,
} from '@/features/setup/SetupCompleteCard';
import {
  blockedLiveWorkId,
  isSetupBlockedByLiveWork,
  setupCancelErrorMessage,
} from '@/features/setup/setupErrorMessage';
import { SetupMap } from '@/features/setup/SetupMap';
import {
  AiZoneDraftsPanel,
  CameraHeightPanel,
  CameraMapPanel,
  CameraPanel,
} from '@/features/setup/SetupPanels';
import { SetupSplit } from '@/features/setup/SetupPreviewModel';
import { pendingReviewCount, type SetupZonePlanView } from '@/features/setup/setupZonePlan';
import { SetupZoneReviewCard } from '@/features/setup/SetupZoneReviewCard';
import { SetupZoneStep } from '@/features/setup/SetupZoneStep';
import { supplementVisible } from '@/features/setup/supplement';
import { SupplementModal } from '@/features/setup/SupplementModal';
import type { SetupMode, ZoneDraftItem } from '@/features/setup/types';
import { useAcceptZoneDraft } from '@/features/setup/useAcceptZoneDraft';
import { useCompleteSetup } from '@/features/setup/useCompleteSetup';
import { useReuseBlockedReason } from '@/features/setup/useReuseScene';
import { reviewsCarriedZones, useSeedSetupZonePlan } from '@/features/setup/useSeedSetupZonePlan';
import { useSetupLayoutSync } from '@/features/setup/useSetupLayoutSync';
import {
  useCameraHeightDefaults,
  useCoverageGuides,
  useCreateSetupSession,
  useResumeSetupSession,
  useRetrySetupSession,
  useSetupSession,
  useSetupZoneDrafts,
} from '@/features/setup/useSetupStep1';
import { useSetupZonePlanQuery } from '@/features/setup/useSetupZonePlan';
import { useWorkAction } from '@/features/work/WorkActionContext';
import { ZoneSourceContext, type SetupZoneSource } from '@/features/zones/zoneSource';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { api } from '@/lib/api';
import { Alert } from '@simai/ui/components/alert';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { Check } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

// 설치 · 보정(명세 8.1·13.4·18.1, S15P31A101-448·-453):
// 처음(이어받을 세션이 없을 때)에는 1단계보다 먼저 「3D 공간」 창으로 새 모델·이전 모델을 고른다. 고르지 않고 닫으면 세팅에서 나간다.
// 1단계: CCTV 4칸 실시간 미리보기, 카메라 각도 헬퍼(2~3초 폴링), 카메라별 실측 높이 입력. 세션이 생기기 전에는 2·3단계로 갈 수 없다.
// 2단계(보정·생성): POST /api/setup-sessions 후 GET /api/setup-sessions/{id} 2초 주기 조회,
// 5단계 막대(배경 수집 → 3D 공간·좌표계 → 카메라 정밀 보정 → 미리보기 맵·구역 초안 → 정밀 3D 맵)·진행률·경과 시간,
// 실패 시 원인·해결 + 다시 시도(POST /api/setup-sessions/{id}/retry)·카메라 연결 관리로, 높이 불일치 시 카메라 다시 맞추기(1단계 복귀 후 재시도),
// 보정 문제로 멈추면(paused_reason) 카메라 연결 관리에서 고친 뒤 '다음' 이 같은 세션을 멈춘 곳부터 이어 간다(POST .../resume),
// 미리보기 맵이 준비되면(85%) 3D 미리보기와 보정 검증 결과 표를 보이고 '다음' 으로 구역 단계에 간다(저절로 넘어가지 않는다).
// 1·2단계는 같은 두 칸 배치(SetupSplit)이고, 2단계 왼쪽은 3D 미리보기 전까지 1단계의 카메라 시야·실시간 영상이다.
// 3단계(구역): 왼쪽 큰 3D(SetupZoneStep)에서 구역 상자를 손잡이로 고치고, 오른쪽에
// 구역 편집 도구(ZonesPage) 및 AI 위험 구역 초안(GET /api/setup-sessions/{id}/zone-drafts, 3D 점선 윤곽, 수락하면 바로 구역 저장·버리기)을 둔다.
// 고화질 정식 Scene 도착 시 자동 교체(편집 초안 유지), 세팅 완료 뒤 작업 관리(/work?workUnitId=) 복귀.
// 단추(다음·다시 보정·세션 취소·카메라 다시 맞추기·작업 관리로)는 setupActions 가 한 곳에서 정한다. 단독 화면(/setup)은 머리줄에 그리고,
// 날짜 화면 안(embedded)은 같은 단추를 아래 오른쪽 단추 줄(useWorkAction)에 등록하며 구역 단계의 마지막 단추는 '다음'(onDone)이다.
// 「맵 보강 촬영(선택)」은 보정·생성의 '다음'에서 뜨는 창이다(SupplementModal, S15P31A101-607): 휴대폰 QR·PC 파일로 사각지대를 찍어 올리고 맵을 다시 굽는다.
// 구역 단계의 마지막 단추가 세팅 완료(C17, S15P31A101-615)를 먼저 부르고(useCompleteSetup) 성공하면 넘어간다: 구역 목록 적용·이 세션 맵 켜기·
// 구성 묶음 발행을 한 번에 한다. 실패하면 구역 편집 아래에 안내하고 머문다. 완료하면 오른쪽 칸에는 「세팅 완료」 상태 카드만 남는다.
// 완료 전에는 관제 Scene 이 바뀌지 않으므로 구역 단계 3D 는 이 세션 맵을 버전으로 받아 그린다(-618 /view, 이전 모델 재사용은 맵이 없어 지금 관제 맵).
// 완료 뒤에는 구역 편집·AI 초안 수락을 감춘다(완료 뒤 구역 수정은 관제 화면 구역 편집).
// 완료 전 구역 단계의 구역 편집·AI 초안 수락·3D 구역 표시는 관제 구역이 아니라 세션 구역 목록(-612) 기준이다(ZoneSourceContext).
// 같은 World 세팅은 구역 단계에 들어오면 빈 목록을 지금 관제 구역으로 한 번 채운다(useSeedSetupZonePlan, 서버 seed): 이전 모델 재사용은
// 그대로, 새 모델 + World 이어짐은 구역마다 '확인 필요'로 채워 오른쪽 칸 「이어 쓴 구역 확인」 카드에서 확인·수정·삭제한다(S15P31A101-629).
// 확인 필요가 남아 있으면 마지막 단추(세팅 완료)가 '확인 필요 n개'를 붙이고 꺼진다. World 가 끊긴 새 모델은 빈 목록에서 시작한다.

/**
 * 날짜 화면 안의 본문 높이: 화면 높이에서 틀(상단 바 60 + 본문 위아래 여백 28 + 날짜 줄 28·간격 14 + 아래 메뉴 자리 96)을 뺀 값이다.
 * 이렇게 묶어 두면 어느 단계에서도 페이지 스크롤이 생기지 않고, 긴 오른쪽 열은 그 안에서만 스크롤한다.
 * 바닥 474 = 단계 줄 42 + 간격 12 + 두 칸 최소 420.
 */
const EMBEDDED_BODY = 'h-[max(474px,calc(100dvh-226px))] flex-none';

const NO_DRAFTS: readonly ZoneDraftItem[] = [];
const NO_GHOSTS: ReturnType<typeof draftGhostsOf> = [];
/** 레이아웃 반영을 하지 않는 화면(날짜 화면 안)이 구역 저장에 넘기는 반영 함수. */
const skipLayoutSync = async () => {};

/** 구역 단계의 '다시 보정'을 눌렀을 때 묻는 글. */
const RESTART_CONFIRM_TEXT = '지금 세팅을 취소하고 다시 보정할까요?';
const RESTART_ZONES_LOST_TEXT = ' 이 세팅의 구역 목록은 새 세팅으로 넘어가지 않습니다.';

/** '다시 보정' 확인 글. 구역 목록은 세션마다 따로라 새 세팅으로 넘어가지 않으니 구역이 있으면 함께 알린다(-612). */
const restartConfirmText = (zoneCount: number): string =>
  zoneCount > 0 ? RESTART_CONFIRM_TEXT + RESTART_ZONES_LOST_TEXT : RESTART_CONFIRM_TEXT;

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

export interface SetupPageProps {
  /** 세팅할 작업. 없으면 작업 관리로 돌려보내는 안내를 보인다. */
  workUnitId?: number;
  /**
   * 날짜 화면(작업 관리) 안에 끼운다: 자기 제목과 머리줄 단추를 그리지 않고 단추를 아래 오른쪽 단추 줄(useWorkAction)에 등록한다.
   * 구역 단계의 마지막 단추는 '다음' 이다(세팅을 완료하고 넘어간다). 단독 화면(/setup)은 머리줄에 단추를 둔다.
   */
  embedded?: boolean;
  /** 구역 단계의 마지막 단추로 세팅을 완료한 뒤. 주면 작업 관리로 이동하지 않고 이것을 부른다(날짜 화면이 다음 단계로 넘긴다). */
  onDone?: () => void;
  /** 처음 뜨는 「3D 공간」 창을 고르지 않고 닫았을 때. 주면 그것을 부르고(날짜 화면은 보고서로 돌아간다), 없으면 작업 관리로 간다. */
  onExit?: () => void;
}

export function SetupPage({ workUnitId, embedded = false, onDone, onExit }: SetupPageProps = {}) {
  if (workUnitId === undefined) return <SetupNeedsWork />;
  return (
    <SetupWorkPage workUnitId={workUnitId} embedded={embedded} onDone={onDone} onExit={onExit} />
  );
}

function SetupWorkPage({
  workUnitId,
  embedded,
  onDone,
  onExit,
}: {
  workUnitId: number;
  embedded: boolean;
  onDone?: () => void;
  onExit?: () => void;
}) {
  // 이어 온 세팅 세션이 있으면 대기 화면에서 시작한다. 맵이 이미 준비됐으면 곧바로 구역 단계로 넘어간다(작업 관리의 구역
  // 단계도 이 화면을 연다).
  const [stage, setStage] = useState<Stage>(() =>
    useSetupFlowStore.getState().activeSessionId === null ? 0 : 1,
  );
  const [heightEdits, setHeightEdits] = useState<Record<string, string>>({});
  const [returnForHeightRetry, setReturnForHeightRetry] = useState(false);
  // 위험 확인창이 열린 동안 보이는 쌍 이름(열 때 붙잡아 둔다: 열린 사이 안내가 바뀌어도 문장이 비지 않는다). 닫혀 있으면 null.
  const [riskConfirmPairs, setRiskConfirmPairs] = useState<readonly string[] | null>(null);
  // 「3D 공간」 창에서 고른 방식. 세션이 없으면 카메라 연결 관리보다 먼저 묻고, '다음' 이 이 방식으로 세션을 만든다.
  const [mode, setMode] = useState<SetupMode | null>(null);
  // 보정·생성의 '다음'을 눌렀을 때 뜨는 맵 보강 촬영 창.
  const [supplementOpen, setSupplementOpen] = useState(false);
  const navigate = useNavigate();

  const activeSessionId = useSetupFlowStore((s) => s.activeSessionId);
  const discardedDraftIds = useSetupFlowStore((s) => s.discardedDraftIds);

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

  // 멈춘 세팅을 고친 높이로 이어 가면 같은 세션이 멈춘 곳부터 돈다(보정·생성으로 돌아간다).
  const resumeSession = useResumeSetupSession(() => {
    setHeightEdits({});
    setReturnForHeightRetry(false);
    go(1);
  });

  const sessionId =
    activeSessionId ??
    retrySession.session?.setup_session_id ??
    createSession.session?.setup_session_id ??
    null;

  // 이어받을 세션이 없다고 확인된 새 세팅이면 카메라 연결 관리보다 먼저 방식을 묻는다.
  const noSessionToResume =
    activeSessionId === null && workUnitSessionQuery.isFetched && !workUnitSessionQuery.data;
  const modePickable = stage === 0 && sessionId === null && mode === null;
  // 이전 모델 쓰기를 켤지는 현재 관제 맵으로 정한다. 창이 뜰 수 있는 때부터 받아 이어받을 세션 조회와 같이 돌린다.
  const reuse = useReuseBlockedReason(modePickable);
  const modelPickerOpen = modePickable && noSessionToResume && !reuse.checking;
  // 이어받을 세션을 찾는 중이거나 창에 쓸 값을 기다리는 중에는 본문을 그리지 않는다: 본문과 창이 같이 뜬다.
  const entryPending =
    (activeSessionId === null && !workUnitSessionQuery.isFetched) ||
    (modePickable && noSessionToResume && reuse.checking);

  // 맵 보강 창이 열려 있는 동안은 끝난 세션도 계속 받는다(휴대폰으로 올린 파일과 다시 굽기 판정이 이 조회로만 온다).
  const sessionQuery = useSetupSession(sessionId, Boolean(sessionId), supplementOpen);
  const currentSession = sessionQuery.data ?? retrySession.session ?? createSession.session ?? null;

  const waiting = useMemo(() => deriveWaitingView(currentSession), [currentSession]);

  // 각도 헬퍼는 1단계에서 주기로 받고, 보정·생성의 왼쪽 카메라 시야 칸(3D 미리보기 전)에서는 받아 둔 값을 쓴다.
  const cameraViewShown = stage === 0 || (stage === 1 && !waiting.previewReady);
  const coverageQuery = useCoverageGuides(cameraViewShown, stage === 0);
  // 보정 검증 쌍별 표·카메라 확인(S15P31A101-646). 통과 전에는 2단계 '다음' 이 꺼진다
  const cameraCheck = useMemo(
    () => deriveCameraCheckView(currentSession, waiting.cameraIds),
    [currentSession, waiting.cameraIds],
  );

  // 이 브라우저에 남은 세션이 다른 작업의 것이면 버리고 이 작업의 세션을 서버에서 다시 찾는다(위 이어받기). 찾기 전에는
  // 1단계에서 기다린다.
  const foreignSession = currentSession !== null && currentSession.work_unit_id !== workUnitId;
  // 날짜 화면 안(embedded)의 날짜 작업에는 레이아웃이 없다(구역이 곧 가상 레이아웃이다). 구역 저장에는 세션 id 를 계속
  // 실어 보내고(setup_session_id) 레이아웃 반영만 건너뛴다.
  const syncsLayout = !embedded;
  const layoutSync = useSetupLayoutSync(
    workUnitId,
    syncsLayout && !foreignSession ? sessionId : null,
  );
  const setupLayoutSync =
    sessionId && !foreignSession
      ? {
          sessionId,
          sync: syncsLayout ? layoutSync.sync : skipLayoutSync,
          retry: syncsLayout ? layoutSync.retry : skipLayoutSync,
        }
      : undefined;
  if (foreignSession && stage !== 0) setStage(0);
  // 1단계를 떠났는데(이어받기 등) 위험 확인창이 남아 있으면 닫는다.
  if (supplementOpen && stage !== 1) setSupplementOpen(false);
  if (riskConfirmPairs !== null && stage !== 0) setRiskConfirmPairs(null);
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
  const sessionRunning = isSetupSessionRunning(currentSession);
  const handleCancelSession = useCallback(() => {
    if (!sessionId || cancelMutation.isPending) return;
    if (!window.confirm('진행 중인 세팅을 취소할까요? 취소하면 처음부터 다시 시작해야 합니다.'))
      return;
    cancelMutation.mutate(sessionId);
  }, [sessionId, cancelMutation]);

  // 구역 단계의 '다시 보정'은 카메라 연결 관리(1단계)부터 다시 시작하므로 세팅 취소처럼 한 번 묻는다.
  // 세팅을 완료했다(C17). 완료 뒤에는 구역 편집·초안 수락을 감춘다
  const completed = setupCompleted(currentSession);
  const completion = useCompleteSetup(currentSession);
  useSeedSetupZonePlan(stage === ZONE_STAGE && !completed ? currentSession : null);

  // 세팅 구역 단계의 구역 편집·초안 수락은 세션 구역 목록에 저장한다(-615). 완료 뒤에는 관제 구역을 본다
  const zoneSource = useMemo<SetupZoneSource | null>(
    () => (sessionId && !foreignSession && !completed ? { sessionId } : null),
    [sessionId, foreignSession, completed],
  );
  // 확인 필요 구역 수(S15P31A101-629). 남아 있으면 세팅을 완료할 수 없다
  const zonePlanQuery = useSetupZonePlanQuery(
    stage === ZONE_STAGE ? (zoneSource?.sessionId ?? null) : null,
  );
  const unconfirmedZones = zoneSource ? pendingReviewCount(zonePlanQuery.data) : 0;
  const draftsEnabled = Boolean(sessionId) && (waiting.previewReady || stage === ZONE_STAGE);
  const zoneDraftsQuery = useSetupZoneDrafts(sessionId, draftsEnabled);
  const draftsList = zoneDraftsQuery.data?.drafts ?? NO_DRAFTS;
  const draftAccept = useAcceptZoneDraft(draftsList, setupLayoutSync, zoneSource);
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

  const restartPending =
    createSession.pending ||
    retrySession.pending ||
    resumeSession.pending ||
    cancelMutation.isPending;

  /**
   * 높이를 고쳐 세팅을 다시 시작한다. 서버 규칙(SetupService)에 맞춰 세션 상태로 가른다: 서버 retry 는 실패한 세션만 받고
   * 끝난 세션은 새로 시작하며, 진행 중인 세션이 있으면 새로 시작할 수 없어 먼저 취소한 뒤 재시도한다.
   */
  const restartWithHeights = useCallback(() => {
    if (!sessionId || !currentSession || !heightValidation.valid || restartPending) return;
    const cameraHeights = heightValidation.cameraHeightsM;
    const retry = () =>
      retrySession.retry({ setupSessionId: sessionId, body: { camera_heights_m: cameraHeights } });
    // 보정 문제로 멈춘 세션은 처음부터가 아니라 멈춘 곳부터 이어 간다.
    if (currentSession.paused_reason && currentSession.status !== 'FAILED')
      resumeSession.resume({
        setupSessionId: sessionId,
        body: { camera_heights_m: cameraHeights },
      });
    else if (currentSession.status === 'FAILED') retry();
    else if (currentSession.status === 'COMPLETED')
      createSession.submit({
        work_unit_id: workUnitId,
        camera_heights_m: cameraHeights,
        mode: currentSession.requested_mode ?? undefined,
      });
    else cancelMutation.mutate(sessionId, { onSuccess: retry });
  }, [
    sessionId,
    currentSession,
    heightValidation,
    restartPending,
    retrySession,
    resumeSession,
    createSession,
    cancelMutation,
    workUnitId,
  ]);

  // mode 를 생략하면 새 모델이다(서버가 NEW 로 본다).
  const createSessionNow = useCallback(
    (requestedMode?: SetupMode) => {
      if (!heightValidation.valid || createSession.pending) return;
      createSession.submit({
        work_unit_id: workUnitId,
        camera_heights_m: heightValidation.cameraHeightsM,
        mode: requestedMode,
      });
    },
    [heightValidation, createSession, workUnitId],
  );

  // 새 세션은 처음에 고른 방식으로 만든다(새 모델은 mode 를 생략한다). 같은 세션의 재시도는 방식이 이미 정해졌으니 묻지 않는다.
  const submitStep1 = useCallback(() => {
    if (!heightValidation.valid) return;
    if (returnForHeightRetry && sessionId) {
      restartWithHeights();
      return;
    }
    if (createSession.pending) return;
    createSessionNow(mode === 'REUSE' ? 'REUSE' : undefined);
  }, [
    heightValidation,
    returnForHeightRetry,
    sessionId,
    restartWithHeights,
    createSession,
    createSessionNow,
    mode,
  ]);

  const exitModelPicker = useCallback(() => {
    if (onExit) onExit();
    else void navigate({ to: '/work', search: { workUnitId: String(workUnitId) } });
  }, [onExit, navigate, workUnitId]);

  // 옆 카메라 쌍의 겹침이 부족하면 보정이 실패할 수 있어 시작 전에 한 번 더 묻는다(높이를 고쳐 다시 시작할 때도 같다).
  const requestStep1Submit = useCallback(() => {
    if (coverage.riskyPairs.length > 0) setRiskConfirmPairs(coverage.riskyPairs);
    else submitStep1();
  }, [coverage.riskyPairs, submitStep1]);

  const confirmRiskySubmit = useCallback(() => {
    setRiskConfirmPairs(null);
    submitStep1();
  }, [submitStep1]);

  const handleRetryFailedSession = useCallback(() => {
    if (!sessionId || retrySession.pending) return;
    retrySession.retry({
      setupSessionId: sessionId,
      body: currentSession?.camera_heights_m
        ? { camera_heights_m: currentSession.camera_heights_m }
        : {},
    });
  }, [sessionId, retrySession, currentSession]);

  /** 카메라 연결 관리(1단계)로 돌아가 고쳐서 다시 시작한다. 실패 안내와 높이 불일치가 같이 쓴다. */
  const returnToStep1 = useCallback(() => {
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

  // 구역 단계의 '다시 보정': 확인을 받고 카메라 연결 관리(0단계)로 돌아간다. 진행 중인 세션은 0단계에서 다시 시도할 때 취소하고 이어 간다.
  const handleRestart = useCallback(() => {
    const zoneCount = sessionId
      ? (queryClient.getQueryData<SetupZonePlanView>(setupZonePlanKey(sessionId))?.items.length ??
        0)
      : 0;
    if (!window.confirm(restartConfirmText(zoneCount))) return;
    returnToStep1();
  }, [sessionId, queryClient, returnToStep1]);

  // 구역 단계의 마지막 단추: 아직 완료 전이면 세팅을 먼저 완료하고(취소·실패면 머문다), 날짜 화면 안이면 그 화면이 다음 단계로 넘기고,
  // 단독 화면이면 그 작업의 작업 관리로 돌아간다.
  const runCompletion = completion.run;
  const finish = useCallback(async () => {
    if (!completed && !(await runCompletion())) return;
    if (onDone) onDone();
    else void navigate({ to: '/work', search: { workUnitId: String(workUnitId) } });
  }, [completed, runCompletion, onDone, navigate, workUnitId]);

  // 보정·생성의 '다음': 보강 촬영을 할 수 있으면(미리보기 맵이 있다) 창으로 먼저 보이고, 아니면 바로 구역 단계로 간다.
  const confirmCalibration = useCallback(() => {
    if (supplementVisible(currentSession)) setSupplementOpen(true);
    else go(ZONE_STAGE);
  }, [currentSession, go]);

  const closeSupplement = useCallback(() => setSupplementOpen(false), []);
  const enterZoneStage = useCallback(() => {
    closeSupplement();
    go(ZONE_STAGE);
  }, [closeSupplement, go]);

  const actions = deriveSetupActions(
    {
      stage,
      embedded,
      startDisabled: !heightValidation.valid || restartPending,
      mapComplete: waiting.mapComplete,
      sessionRunning,
      cancelPending: cancelMutation.isPending,
      showHeightNotice: waiting.showHeightNotice,
      canFinish: completed || canCompleteSetup(currentSession),
      finishPending: completion.pending,
      setupCompleted: completed,
      unconfirmedZones: completed ? 0 : unconfirmedZones,
      cameraCheckBlocked: completed ? null : cameraCheckBlockReason(cameraCheck),
      cameraCheckFailed: !waiting.failed && cameraCheckFailed(cameraCheck),
    },
    {
      start: requestStep1Submit,
      confirm: confirmCalibration,
      restart: handleRestart,
      cancelSession: handleCancelSession,
      recheckCameras: returnToStep1,
      finish: () => void finish(),
    },
  );
  useWorkAction(embedded && !entryPending ? toWorkAction(actions) : null);

  const step1RawError =
    resumeSession.rawError ??
    retrySession.rawError ??
    createSession.rawError ??
    cancelMutation.error;
  // 취소 실패는 그 취소를 요청한 세션 화면에서만 보인다(다시 시도로 새 세션이 되면 지난 실패를 지운다)
  const cancelError =
    cancelMutation.error && cancelMutation.variables === sessionId
      ? setupCancelErrorMessage(cancelMutation.error)
      : null;
  const step1Error =
    resumeSession.error ?? retrySession.error ?? createSession.error ?? cancelError;
  // 막는 세팅을 취소했으니 거절 오류를 지워 다시 시도할 수 있게 한다.
  const clearStep1Error = () => {
    createSession.reset();
    retrySession.reset();
    cancelMutation.reset();
  };
  const step1BlockedByLiveWork = isSetupBlockedByLiveWork(step1RawError);
  const retryBlockedByLiveWork = isSetupBlockedByLiveWork(retrySession.rawError);

  const navigateToBlockedWork = useCallback(
    (error: unknown) => {
      const liveWorkId = blockedLiveWorkId(error);
      if (liveWorkId !== null) {
        void navigate({ to: '/work', search: { workUnitId: String(liveWorkId) } });
      } else {
        void navigate({ to: '/work' });
      }
    },
    [navigate],
  );

  const bodyClass = cn('flex flex-col gap-3', embedded ? EMBEDDED_BODY : 'min-h-0 flex-1');
  if (entryPending) return <div className={bodyClass} />;

  return (
    <div className={bodyClass}>
      {!embedded && <PageTitle>설치 · 보정</PageTitle>}
      {!embedded && (
        <div className="flex flex-wrap justify-end gap-2">
          {actions.map((action) => (
            <Button
              key={action.key}
              size="lg"
              variant={action.primary ? undefined : 'secondary'}
              className={cn('px-4', !action.primary && 'bg-surface-3 hover:bg-surface-3/70')}
              disabled={action.disabled}
              title={action.title}
              onClick={action.onClick}
            >
              {action.label}
            </Button>
          ))}
        </div>
      )}

      <ol aria-label="보정 단계" className="grid grid-cols-3 gap-2">
        {STEPS.map((label, k) => {
          const done = k < stage;
          const cur = k === stage;
          return (
            <li
              key={label}
              aria-current={cur ? 'step' : undefined}
              className={cn(
                'flex items-center gap-2.5 rounded-lg bg-card px-3.5 py-2.5 text-sm text-muted-foreground',
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
            </li>
          );
        })}
      </ol>

      {stage === 0 ? (
        <SetupSplit left={<CameraMapPanel coverage={coverage} />}>
          <CameraPanel coverage={coverage} />
          <CameraHeightPanel
            fields={heightFields}
            onChange={onChangeHeight}
            submitError={step1Error}
            onNavigateLiveWork={
              step1BlockedByLiveWork ? () => navigateToBlockedWork(step1RawError) : undefined
            }
            errorAction={
              <BlockingSetupNotice error={step1RawError} onCancelled={clearStep1Error} />
            }
            heightWarnings={waiting.heightWarnings}
            retrySessionId={returnForHeightRetry ? sessionId : null}
            onRetryWithHeights={requestStep1Submit}
            retryLabel={currentSession?.paused_reason ? '이어서 만들기' : undefined}
            retryPending={restartPending}
            canSubmitHeights={heightValidation.valid}
          />
          {riskConfirmPairs !== null && (
            <CameraRiskDialog
              riskyPairs={riskConfirmPairs}
              onConfirm={confirmRiskySubmit}
              onCancel={() => setRiskConfirmPairs(null)}
            />
          )}
        </SetupSplit>
      ) : stage === 1 ? (
        currentSession && (
          <SetupCalibrateStep
            waiting={waiting}
            cameraView={<CameraMapPanel coverage={coverage} />}
            cameraCheck={cameraCheck}
            showCameraCheck={
              cameraCheck.gated && Boolean(currentSession.calibration_version) && !waiting.failed
            }
            onRecalibrate={handleRestart}
            pollingError={sessionQuery.error}
            onRetry={handleRetryFailedSession}
            retryPending={retrySession.pending}
            retryError={retrySession.error ?? cancelError}
            onNavigateLiveWork={
              retryBlockedByLiveWork
                ? () => navigateToBlockedWork(retrySession.rawError)
                : undefined
            }
            onReturnToStep1={returnToStep1}
          />
        )
      ) : (
        <ZoneSourceContext value={zoneSource}>
          <SetupZoneStep
            title={zoneMapTitle(waiting.sceneTier)}
            sceneVersion={sessionSceneVersion(waiting.sceneTier)}
            draftGhosts={completed ? NO_GHOSTS : draftGhosts}
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
            {layoutSync.failure && (
              <div role="alert">
                구역은 발행됐지만 가상 레이아웃 반영에 실패했습니다.
                <Button disabled={layoutSync.pending} onClick={() => void layoutSync.retry()}>
                  다시 시도
                </Button>
              </div>
            )}
            {zoneSource && (reviewsCarriedZones(currentSession) || unconfirmedZones > 0) && (
              <SetupZoneReviewCard sessionId={zoneSource.sessionId} />
            )}
            {!completed && (
              <AiZoneDraftsPanel
                draftsResponse={zoneDraftsQuery.data}
                acceptedDraftIds={acceptedDraftIds}
                discardedDraftIds={discardedDraftIds}
                pendingDraftId={draftAccept.pendingId}
                acceptFailure={draftAccept.failure}
                onAcceptDraft={(item) => void draftAccept.accept(item)}
                onDiscardDraft={discardSetupZoneDraft}
              />
            )}
            {currentSession && !completed && (
              <ZonesPage placement="inline" addOnDemand setupLayoutSync={setupLayoutSync} />
            )}
            {!completed && completion.error && (
              <Alert role="alert" className="text-xs">
                {completion.error}
              </Alert>
            )}
            {currentSession && (
              <SetupCompleteCard session={currentSession} applied={completion.applied} />
            )}
          </SetupZoneStep>
        </ZoneSourceContext>
      )}
      {modelPickerOpen && (
        <ModelPickerDialog
          onNew={() => setMode('NEW')}
          onReuse={() => setMode('REUSE')}
          onClose={exitModelPicker}
          reuseBlockedReason={reuse.reason}
        />
      )}
      {supplementOpen && currentSession && (
        <SupplementModal
          session={currentSession}
          onNext={enterZoneStage}
          onCancel={closeSupplement}
          onRebaked={closeSupplement}
        />
      )}
    </div>
  );
}
