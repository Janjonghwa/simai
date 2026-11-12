import {
  COVERAGE_GUIDE_POLL_MS,
  createSetupSession,
  requestCameraHeightDefaults,
  requestCoverageGuides,
  requestSetupSession,
  requestSetupZoneDrafts,
  retrySetupSession,
  SETUP_COVERAGE_GUIDES_KEY,
  SETUP_HEIGHT_DEFAULTS_KEY,
  SETUP_SESSION_POLL_MS,
  setupSessionKey,
  setupZoneDraftsKey,
} from '@/features/setup/api';
import { setActiveSetupSessionId } from '@/features/setup/setup.store';
import { setupErrorMessage } from '@/features/setup/setupErrorMessage';
import { rebakeRunning } from '@/features/setup/supplement';
import type {
  CreateSetupSessionBody,
  RetrySetupSessionBody,
  SetupSessionResponse,
  SetupZoneDraftsResponse,
} from '@/features/setup/types';
import { api } from '@/lib/api';
import { useCameraOrderStore } from '@/lib/cameraNames';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

// 세팅 1단계 및 2단계 이후 서버 데이터 훅(각도 헬퍼 폴링, 카메라 높이 기본값, 세팅 세션 시작·2초 주기 조회·재시도, AI 구역 초안 조회).

export function useCoverageGuides(enabled = true) {
  const query = useQuery({
    queryKey: SETUP_COVERAGE_GUIDES_KEY,
    queryFn: async ({ signal }) => {
      const data = await requestCoverageGuides(api, signal);
      const ids = data.latest?.cameras.map((c) => c.camera_id) ?? [];
      if (ids.length) useCameraOrderStore.getState().set(ids);
      return data;
    },
    enabled,
    refetchInterval: enabled ? COVERAGE_GUIDE_POLL_MS : false,
  });
  return {
    data: query.data,
    pending: query.isPending,
    error: query.isError,
  };
}

export function useCameraHeightDefaults(enabled = true) {
  const query = useQuery({
    queryKey: SETUP_HEIGHT_DEFAULTS_KEY,
    queryFn: ({ signal }) => requestCameraHeightDefaults(api, signal),
    enabled,
  });
  return {
    data: query.data,
    pending: query.isPending,
    error: query.isError,
  };
}

export function submitErrorText(error: unknown): string | null {
  if (!error) return null;
  return setupErrorMessage(error);
}

export function useCreateSetupSession(onCreated?: (session: SetupSessionResponse) => void) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (body: CreateSetupSessionBody) => createSetupSession(api, body),
    onSuccess: (session) => {
      queryClient.setQueryData(setupSessionKey(session.setup_session_id), session);
      setActiveSetupSessionId(session.setup_session_id);
      onCreated?.(session);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: SETUP_HEIGHT_DEFAULTS_KEY });
    },
  });

  return {
    submit: mutation.mutate,
    pending: mutation.isPending,
    error: submitErrorText(mutation.error),
    rawError: mutation.error,
    session: mutation.data ?? null,
    reset: mutation.reset,
  };
}

function isTerminalSession(session: SetupSessionResponse | undefined): boolean {
  if (!session) return false;
  // 끝난 세션이어도 맵 다시 굽기(S15P31A101-607)가 진행 중이면 계속 조회한다
  if (rebakeRunning(session)) return false;
  const stage = session.stage ?? session.status;
  return (
    session.status === 'COMPLETED' ||
    session.status === 'FAILED' ||
    stage === 'COMPLETED' ||
    stage === 'FAILED'
  );
}

/**
 * 세팅 시작 뒤 GET /api/setup-sessions/{id} 를 2초 주기로 조회한다.
 * 미리보기 Scene(preview_scene_version)·정식 고화질 Scene(scene_version)이 도착하면 세팅 화면은 그 버전을
 * GET /api/scenes/{scene_version}/view 로 새로 받아 뷰어를 자동 교체한다(편집 중인 초안은 유지). 지금 관제 Scene 은 보지 않는다.
 * 끝난 세션은 조회를 멈추지만 keepPolling 이면 계속 받는다(맵 보강 창이 열려 있는 동안 휴대폰으로 올린 파일·다시 굽기 판정을 따라간다).
 */
export function useSetupSession(sessionId: string | null, enabled = true, keepPolling = false) {
  const active = Boolean(sessionId) && enabled;
  const query = useQuery({
    queryKey: setupSessionKey(sessionId ?? ''),
    queryFn: ({ signal }) => requestSetupSession(api, sessionId!, signal),
    enabled: active,
    refetchInterval: (q) => {
      if (!active) return false;
      return isTerminalSession(q.state.data) && !keepPolling ? false : SETUP_SESSION_POLL_MS;
    },
  });

  return {
    data: query.data ?? null,
    pending: query.isPending,
    error: query.isError,
    refetch: query.refetch,
  };
}

export interface RetrySetupSessionVars {
  setupSessionId: string;
  body?: RetrySetupSessionBody;
}

export function useRetrySetupSession(onRetried?: (session: SetupSessionResponse) => void) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: ({ setupSessionId, body }: RetrySetupSessionVars) =>
      retrySetupSession(api, setupSessionId, body),
    onSuccess: (session) => {
      queryClient.setQueryData(setupSessionKey(session.setup_session_id), session);
      setActiveSetupSessionId(session.setup_session_id);
      onRetried?.(session);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: SETUP_HEIGHT_DEFAULTS_KEY });
    },
  });

  return {
    retry: mutation.mutate,
    pending: mutation.isPending,
    error: submitErrorText(mutation.error),
    rawError: mutation.error,
    session: mutation.data ?? null,
    reset: mutation.reset,
  };
}

function isDraftRunPending(data: SetupZoneDraftsResponse | undefined): boolean {
  if (!data) return true;
  return data.status === 'NOT_REQUESTED' || data.status === 'QUEUED';
}

/** GET /api/setup-sessions/{id}/zone-drafts 조회 훅. 아직 QUEUED/NOT_REQUESTED 면 2초 주기로 다시 확인한다. */
export function useSetupZoneDrafts(sessionId: string | null, enabled = true) {
  const active = Boolean(sessionId) && enabled;
  const query = useQuery({
    queryKey: setupZoneDraftsKey(sessionId ?? ''),
    queryFn: ({ signal }) => requestSetupZoneDrafts(api, sessionId!, signal),
    enabled: active,
    refetchInterval: (q) => {
      if (!active) return false;
      return isDraftRunPending(q.state.data) ? SETUP_SESSION_POLL_MS : false;
    },
  });

  return {
    data: query.data ?? null,
    pending: query.isPending,
    error: query.isError,
    refetch: query.refetch,
  };
}
