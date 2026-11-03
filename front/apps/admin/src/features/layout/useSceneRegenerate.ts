import {
  isSceneRegenerateInProgress,
  requestCommitScene,
  requestCreateSceneRegenerate,
  requestLatestSceneRegenerate,
  requestSceneRegenerateById,
  SCENE_REGENERATE_LATEST_KEY,
  SCENE_REGENERATE_POLL_INTERVAL_MS,
  type SceneRegenerateView,
} from '@/features/layout/api';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';

export interface UseSceneRegenerateResult {
  current: SceneRegenerateView | null;
  isInProgress: boolean;
  isSubmitting: boolean;
  isActivating: boolean;
  activatedSceneVersion: string | null;
  requestError: string | null;
  regenerate: (reason?: string) => Promise<void>;
  activateCandidate: () => Promise<void>;
  clearError: () => void;
}

export function useSceneRegenerate(enabled: boolean = true): UseSceneRegenerateResult {
  const queryClient = useQueryClient();
  const [polledView, setPolledView] = useState<SceneRegenerateView | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isActivating, setIsActivating] = useState(false);
  const [activatedSceneVersion, setActivatedSceneVersion] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);

  const latestQuery = useQuery({
    queryKey: SCENE_REGENERATE_LATEST_KEY,
    queryFn: ({ signal }) => requestLatestSceneRegenerate(api, signal),
    enabled,
    retry: false,
  });

  const current: SceneRegenerateView | null = polledView ?? latestQuery.data ?? null;
  const activeRequestId =
    current && isSceneRegenerateInProgress(current.status) ? current.request_id : null;

  const pollOnce = useCallback(
    async (requestId: number) => {
      try {
        const next = await requestSceneRegenerateById(api, requestId);
        setPolledView(next);
        if (!isSceneRegenerateInProgress(next.status)) {
          queryClient.setQueryData(SCENE_REGENERATE_LATEST_KEY, next);
        }
      } catch {
        // 폴링 중 일시 오류는 다음 2초 주기에 재시도한다.
      }
    },
    [queryClient],
  );

  useEffect(() => {
    if (!enabled || activeRequestId === null) return;
    const timer = window.setInterval(() => {
      void pollOnce(activeRequestId);
    }, SCENE_REGENERATE_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [enabled, activeRequestId, pollOnce]);

  const regenerate = useCallback(
    async (reason?: string) => {
      if (!enabled || isSubmitting) return;
      setIsSubmitting(true);
      setRequestError(null);
      try {
        const accepted = await requestCreateSceneRegenerate(api, reason);
        const initial: SceneRegenerateView = {
          request_id: accepted.request_id,
          status: accepted.status,
          failure_reason: null,
          scene_version: null,
          reason: reason?.trim() || null,
          requested_at_ms: Date.now(),
          updated_at_ms: Date.now(),
        };
        setPolledView(initial);
        queryClient.setQueryData(SCENE_REGENERATE_LATEST_KEY, initial);
      } catch (err) {
        if (err instanceof ApiError && err.code === 'REGENERATE_IN_PROGRESS') {
          setRequestError('이미 진행 중인 맵 재생성 요청이 있습니다.');
          if (err.requestId) {
            await pollOnce(err.requestId);
          } else {
            await queryClient.invalidateQueries({ queryKey: SCENE_REGENERATE_LATEST_KEY });
          }
        } else if (err instanceof ApiError && err.code === 'INVALID_REGENERATE_REQUEST') {
          setRequestError('재생성 사유는 120자 이하로 입력해 주세요.');
        } else {
          setRequestError('맵 재생성 요청에 실패했습니다. 잠시 후 다시 시도해 주세요.');
        }
      } finally {
        setIsSubmitting(false);
      }
    },
    [enabled, isSubmitting, pollOnce, queryClient],
  );

  const activateCandidate = useCallback(async () => {
    const candidateVersion = current?.scene_version;
    if (!enabled || !candidateVersion || current?.status !== 'CANDIDATE_READY' || isActivating) {
      return;
    }
    setIsActivating(true);
    setRequestError(null);
    try {
      await requestCommitScene(api, candidateVersion);
      setActivatedSceneVersion(candidateVersion);
      await queryClient.invalidateQueries({ queryKey: ['scenes', 'current'] });
    } catch {
      setRequestError('새 3D 배경 후보 활성화에 실패했습니다.');
    } finally {
      setIsActivating(false);
    }
  }, [current?.scene_version, current?.status, enabled, isActivating, queryClient]);

  const clearError = useCallback(() => {
    setRequestError(null);
  }, []);

  return {
    current,
    isInProgress: isSceneRegenerateInProgress(current?.status),
    isSubmitting,
    isActivating,
    activatedSceneVersion,
    requestError,
    regenerate,
    activateCandidate,
    clearError,
  };
}
