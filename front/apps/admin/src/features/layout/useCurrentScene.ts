import { requestCurrentScene } from '@/features/layout/api';
import type { ViewerSceneInput, ViewerSceneStatus } from '@/features/viewer3d';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useRef, useState } from 'react';

/** 조회 실패·자산 권한 거부가 계속돼도 요청이 몰리지 않게 하는 간격(ms). */
const RELOAD_MS = 10_000;

/** 현재 배경. 조회 실패에는 마지막 본문을 유지하고, 처음 받기 전에는 기본 바닥을 쓴다. */
export function useCurrentScene() {
  const [sceneStatus, setSceneStatus] = useState<ViewerSceneStatus | null>(null);
  const [needsRefetch, setNeedsRefetch] = useState(false);
  const nextRefetchAt = useRef(0);
  const { data, refetch } = useQuery({
    queryKey: ['scenes', 'current'],
    queryFn: async ({ signal }): Promise<ViewerSceneInput> => ({
      ok: true,
      raw: await requestCurrentScene(api, signal),
    }),
    staleTime: Infinity,
    // 호스트를 다시 열 때는 만료됐을 수 있는 서명 주소를 재사용하지 않는다.
    gcTime: 0,
    refetchInterval: (q) => (q.state.status === 'error' || needsRefetch ? RELOAD_MS : false),
    refetchIntervalInBackground: true,
  });

  const onSceneStatusChange = useCallback(
    (status: ViewerSceneStatus) => {
      setSceneStatus(status);
      setNeedsRefetch(status.needsRefetch);
      if (!status.needsRefetch || Date.now() < nextRefetchAt.current) return;
      // 첫 만료·권한 거부는 즉시 받는다. 같은 신호가 반복되면 위 주기 조회로 회복한다.
      nextRefetchAt.current = Date.now() + RELOAD_MS;
      void refetch({ cancelRefetch: false });
    },
    [refetch],
  );

  return { scene: data ?? null, sceneStatus, onSceneStatusChange };
}
