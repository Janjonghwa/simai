import { requestCurrentScene } from '@/features/layout/api';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';

// 방식 고르기 창의 「이전 모델 쓰기」를 켜도 되는지 정하는 조회. 쓸 수 있는 이전 모델은 확정된 현재 관제 맵 하나다.
// 카메라 위치가 달라졌어도 막지 않는다 — 이전 모델 쓰기의 보정은 카메라를 그 모델에 맞춘다.

const REUSE_SCENE_KEY = ['setup', 'reuse-scene'] as const;

const CHECK_FAILED = '이전 모델을 확인하지 못했어요';
const NO_SCENE = '이전 모델이 없어요';

/** 현재 Scene 응답에서 이 판단에 쓰는 칸만 읽는다(배경 본문 검사는 뷰어가 맡는다). */
const currentSceneSchema = z.object({ state: z.string() });

/** 현재 관제 맵 응답(GET /api/scenes/current)에서 이전 모델을 쓸 수 없는 이유. 쓸 수 있으면 null. */
function reuseBlockedReason(raw: unknown): string | null {
  const parsed = currentSceneSchema.safeParse(raw);
  if (!parsed.success) return CHECK_FAILED;
  return parsed.data.state === 'COMMITTED' ? null : NO_SCENE;
}

export interface ReuseCheck {
  /** 첫 응답을 기다리는 중이다. 창은 이것이 끝난 뒤에 열어 안의 글자가 바뀌지 않게 한다. */
  checking: boolean;
  /** 이전 모델을 쓸 수 없는 이유. 쓸 수 있으면 null. */
  reason: string | null;
}

/** 켜져 있는 동안 현재 맵을 받아, 이전 모델을 쓸 수 있는지 돌려준다. 창이 뜨기 전부터 켜 두면 창이 뜰 때 결과가 이미 있다. */
export function useReuseBlockedReason(enabled: boolean): ReuseCheck {
  const query = useQuery({
    queryKey: REUSE_SCENE_KEY,
    queryFn: async ({ signal }) => {
      try {
        return reuseBlockedReason(await requestCurrentScene(api, signal));
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return NO_SCENE;
        throw error;
      }
    },
    enabled,
  });
  if (query.isPending) return { checking: true, reason: null };
  if (query.isError) return { checking: false, reason: CHECK_FAILED };
  return { checking: false, reason: query.data ?? null };
}
