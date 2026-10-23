import { requestClipRetention } from '@/features/clips/api';
import { clipsKey } from '@/features/clips/useEventClips';
import { api } from '@/lib/api';
import { useMutation, useQueryClient } from '@tanstack/react-query';

/**
 * 클립 하나의 보존 표시 붙이기·떼기(관리자, 명세 11.5 아차사고). 낙관적 갱신을 하지 않는다(CONVENTION).
 * 성공하든 실패하든 목록을 다시 받아 서버가 가진 값으로 그린다.
 */
export function useClipRetention(eventId: string, clipId: string) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (hold: boolean) => requestClipRetention(api, clipId, hold),
    onSettled: () => queryClient.invalidateQueries({ queryKey: clipsKey(eventId) }),
  });
  return {
    setRetention: (hold: boolean) => mutation.mutate(hold),
    pending: mutation.isPending,
    error: mutation.isError ? '보존 표시를 바꾸지 못했습니다' : undefined,
  };
}
