import { requestStaticObjects, STATIC_OBJECTS_KEY } from '@/features/surfaces/api';
import { readWorkSurfaces, toViewerWorkSurfaces } from '@/features/surfaces/surfaces';
import type { ViewerWorkSurfaces, WorkSurface } from '@/features/surfaces/types';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';

const NONE: WorkSurface[] = [];

/** 작업면을 받지 못했을 때 다시 시도하는 간격(ms). */
const RELOAD_MS = 10_000;

/**
 * 등록 작업면. 로그인마다 한 번 받고, 받지 못하면 받을 때까지 다시 시도한다. 저장하면 응답으로 목록을 바꾼다.
 * raw 는 3D 뷰어 Source 에 넘길 묶음이다(뷰어가 다시 검사한다). 받기 전·실패면 없다(빈 목록으로 3D 를 비우지 않는다).
 */
export function useWorkSurfaces() {
  const query = useQuery({
    queryKey: STATIC_OBJECTS_KEY,
    queryFn: ({ signal }) => requestStaticObjects(api, signal),
    staleTime: Infinity,
    refetchInterval: (q) => (q.state.data ? false : RELOAD_MS),
    refetchIntervalInBackground: true,
  });
  const surfaces = useMemo(() => (query.data ? readWorkSurfaces(query.data) : NONE), [query.data]);
  const raw = useMemo<ViewerWorkSurfaces | undefined>(
    () => (query.data ? toViewerWorkSurfaces(surfaces) : undefined),
    [query.data, surfaces],
  );
  return { surfaces, raw, loaded: query.data !== undefined, failed: query.isError };
}
