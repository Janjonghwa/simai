import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { requestVideoSegments } from '@/features/history/api';
import { activeSegments } from '@/features/history/replaySegments';
import type { Span, VideoSegment } from '@/features/history/types';
import type { ReplaySpatialSource } from '@/features/viewer3d';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useMemo, useSyncExternalStore } from 'react';

const NONE: VideoSegment[] = [];

/**
 * 작업 구간과 겹치는 상시 영상 조각 목록. 영상은 관리자만 볼 수 있어(명세 14장) 관리자일 때만 받는다.
 * 구간 기록은 다 지난 일이라 한 번만 받는다. 조각이 없거나 받지 못하면 빈 목록이라 목 화면 그림이 그대로 보인다.
 */
export function useReplaySegments(range: Span): VideoSegment[] {
  const admin = useCurrentRole() === 'ADMIN';
  const query = useQuery({
    queryKey: ['history', 'video-segments', range.from, range.to],
    queryFn: ({ signal }) => requestVideoSegments(api, range, signal),
    enabled: admin,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });
  return query.data?.segments ?? NONE;
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
