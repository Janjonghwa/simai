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
import type {
  CreateSetupSessionBody,
  RetrySetupSessionBody,
  SetupSessionResponse,
  SetupZoneDraftsResponse,
} from '@/features/setup/types';
import { api } from '@/lib/api';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';

// 세팅 1단계 및 2단계 이후 서버 데이터 훅(각도 헬퍼 폴링, 카메라 높이 기본값, 세팅 세션 시작·2초 주기 조회·재시도, AI 구역 초안 조회).

export function useCoverageGuides(enabled = true) {
  const query = useQuery({
    queryKey: SETUP_COVERAGE_GUIDES_KEY,
    queryFn: ({ signal }) => requestCoverageGuides(api, signal),
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
 * 미리보기 Scene(preview_scene_version) 또는 정식 고화질 Scene(scene_version)이 새로 도착하면
 * 현재 Scene 쿼리(['scenes', 'current'])를 무효화해 뷰어가 자동 교체하도록 한다(편집 중인 초안은 유지).
 */
export function useSetupSession(sessionId: string | null, enabled = true) {
  const queryClient = useQueryClient();
  const active = Boolean(sessionId) && enabled;
  const query = useQuery({
    queryKey: setupSessionKey(sessionId ?? ''),
    queryFn: ({ signal }) => requestSetupSession(api, sessionId!, signal),
    enabled: active,
    refetchInterval: (q) => {
      if (!active) return false;
      return isTerminalSession(q.state.data) ? false : SETUP_SESSION_POLL_MS;
    },
  });

  const lastPreviewSceneRef = useRef<string | null>(null);
  const lastFinalSceneRef = useRef<string | null>(null);

  useEffect(() => {
    const data = query.data;
    if (!data) return;
    const nextPreview = data.preview_scene_version ?? null;
    const nextFinal = data.scene_version ?? null;
    const previewChanged = nextPreview !== null && nextPreview !== lastPreviewSceneRef.current;
    const finalChanged = nextFinal !== null && nextFinal !== lastFinalSceneRef.current;
    lastPreviewSceneRef.current = nextPreview;
    lastFinalSceneRef.current = nextFinal;
    if (previewChanged || finalChanged) {
      void queryClient.invalidateQueries({ queryKey: ['scenes', 'current'] });
    }
  }, [query.data, queryClient]);

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
