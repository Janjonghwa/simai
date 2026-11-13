import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { requestVideoSegments } from '@/features/history/api';
import { activeSegments } from '@/features/history/replaySegments';
import type { Span, VideoSegment } from '@/features/history/types';
import type { ReplaySpatialSource } from '@/features/viewer3d';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react';

const NONE: VideoSegment[] = [];

/** 10초 간격 최대 60회. 완료 뒤에도 다시 화면을 열면 목록을 재검증한다. */
export const VIDEO_POLL_MS = 10_000;
export const VIDEO_POLL_LIMIT = 60;

export function useReplaySegments(range: Span) {
  const admin = useCurrentRole() === 'ADMIN';
  const started = useRef(0);
  useEffect(() => {
    started.current = Date.now();
  }, [range.from, range.to]);
  const query = useQuery({
    queryKey: ['history', 'video-segments', range.from, range.to],
    queryFn: ({ signal }) => requestVideoSegments(api, range, signal),
    enabled: admin,
    staleTime: 0,
    retry: false,
    refetchInterval: (current) => {
      const error = current.state.error;
      if (error instanceof ApiError && [401, 403, 410].includes(error.status)) return false;
      const uploads = current.state.data?.recording_uploads;
      const terminal =
        uploads &&
        uploads.length > 0 &&
        uploads.every((item) => ['COMPLETED', 'NO_RECORDING', 'EXPIRED'].includes(item.status));
      if (Date.now() - started.current >= VIDEO_POLL_MS * VIDEO_POLL_LIMIT) return false;
      if (
        terminal ||
        current.state.dataUpdateCount + current.state.errorUpdateCount >= VIDEO_POLL_LIMIT
      )
        return false;
      return VIDEO_POLL_MS;
    },
  });
  const segments = admin ? (query.data?.segments ?? NONE) : NONE;
  const uploads = query.data?.recording_uploads ?? [];
  let message: string | null = null;
  if (!admin || (query.error instanceof ApiError && query.error.status === 403))
    message = '영상 조회 권한이 없습니다. 관리자 계정으로 확인해 주세요.';
  else if (query.error instanceof ApiError && query.error.status === 401)
    message = '로그인이 만료되었습니다. 다시 로그인해 주세요.';
  else if (query.error instanceof ApiError && query.error.status === 410)
    message = '영상 보관 기간이 만료되었습니다.';
  else if (query.error)
    message = '영상 목록을 불러오지 못했습니다. 네트워크 연결을 확인하고 다시 확인해 주세요.';
  else if (uploads.some((item) => item.status === 'FAILED'))
    message = '영상 업로드에 실패했거나 진행 보고가 끊겼습니다. 3D 기록을 먼저 볼 수 있습니다.';
  else if (uploads.some((item) => ['WAITING', 'UPLOADING'].includes(item.status)))
    message = '영상 업로드 중 · 3D 기록 먼저 보기';
  else if (uploads.length > 0 && uploads.every((item) => item.status === 'EXPIRED'))
    message = '영상 보관 기간이 만료되었습니다.';
  else if (uploads.length > 0 && uploads.every((item) => item.status === 'NO_RECORDING'))
    message = '이 작업 구간에 녹화된 영상이 없습니다.';
  else if (segments.length > 0 && segments.every((item) => item.deleted_at_ms != null))
    message = '영상 보관 기간이 만료되었습니다.';
  else if (!query.isPending && segments.length === 0)
    message = '녹화 영상을 확인 중입니다. 3D 기록을 먼저 볼 수 있습니다.';
  return { segments, message, refresh: query.refetch, canRefresh: admin };
}

/**
 * 지금 재생 시각을 덮는 조각(카메라별). 재생 시각은 0.1초마다 바뀌지만 덮는 조각이 바뀔 때만 다시 그린다.
 * 같은 조각 집합이면 같은 배열이다.
 */
export function useActiveSegments(
  replay: ReplaySpatialSource | null,
  segments: readonly VideoSegment[],
): VideoSegment[] {
  const subscribe = useCallback(
    (listener: () => void) => (replay ? replay.subscribeStatus(listener) : () => {}),
    [replay],
  );
  const key = useSyncExternalStore(subscribe, () =>
    activeSegments(segments, replay?.getStatus().playhead ?? null)
      .map((segment) => segment.segment_id)
      .join(','),
  );
  return useMemo(() => {
    const ids = new Set(key === '' ? [] : key.split(','));
    return segments
      .filter((segment) => ids.has(segment.segment_id))
      .sort((a, b) => a.camera_id.localeCompare(b.camera_id));
  }, [key, segments]);
}
