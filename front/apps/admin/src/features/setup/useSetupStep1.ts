import {
  COVERAGE_GUIDE_POLL_MS,
  createSetupSession,
  requestCameraHeightDefaults,
  requestCoverageGuides,
  SETUP_COVERAGE_GUIDES_KEY,
  SETUP_HEIGHT_DEFAULTS_KEY,
} from '@/features/setup/api';
import type { CreateSetupSessionBody, SetupSessionResponse } from '@/features/setup/types';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

// 세팅 1단계 서버 데이터 훅(각도 헬퍼 2~3초 폴링, 카메라 높이 기본값 조회, 세팅 세션 시작).

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

function submitErrorText(error: unknown): string | null {
  if (!error) return null;
  if (
    error instanceof ApiError &&
    error.status >= 400 &&
    error.status < 500 &&
    error.serverMessage
  ) {
    return error.serverMessage;
  }
  return '현장 세팅을 시작하지 못했습니다. 잠시 후 다시 시도해 주세요';
}

export function useCreateSetupSession(onCreated?: (session: SetupSessionResponse) => void) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (body: CreateSetupSessionBody) => createSetupSession(api, body),
    onSuccess: (session) => {
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
    session: mutation.data ?? null,
    reset: mutation.reset,
  };
}
