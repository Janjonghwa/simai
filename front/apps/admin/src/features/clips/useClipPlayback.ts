import { requestClipUrl } from '@/features/clips/api';
import { api } from '@/lib/api';
import { useMutation } from '@tanstack/react-query';
import { useRef, useState } from 'react';

/** 주소가 이 시간 안에 만료되면 새로 받는다(ms). EC2 시계와 이 브라우저 시계의 차이를 넉넉히 본다. */
const EXPIRY_MARGIN_MS = 5_000;

/**
 * 주소를 받은 지 이 시간(ms)이 지난 뒤의 재생 실패는 주소 만료(되감기·이어 받기 중)로 보고 다시 받는다.
 * 그보다 빠른 실패는 한 번만 다시 받는다. 영상이 계속 깨져도 주소를 이 간격보다 자주 받지 않는다.
 */
const STALE_AFTER_MS = 30_000;

/**
 * 클립 재생 주소. 재생을 누를 때만 받는다. EC2 가 주소를 줄 때 조회 기록을 남기므로(명세 14장)
 * 상세를 열기만 해서는 받지 않는다. 받은 주소가 만료됐으면 새로 받고, 재생이 실패하면 위 규칙대로 다시 받는다.
 */
export function useClipPlayback(clipId: string) {
  const mutation = useMutation({ mutationFn: () => requestClipUrl(api, clipId) });
  const retried = useRef(false);
  const [playFailed, setPlayFailed] = useState(false);
  // 재생을 누를 때마다 는다. 영상을 새로 띄워 처음부터 다시 재생한다(주소가 살아 있으면 주소는 그대로 쓴다).
  const [round, setRound] = useState(0);

  const play = () => {
    retried.current = false;
    setPlayFailed(false);
    setRound((n) => n + 1);
    const data = mutation.data;
    if (!data || data.expires_at_ms - EXPIRY_MARGIN_MS <= Date.now()) mutation.mutate();
  };

  /** 재생이 실패했을 때(주소 만료 등) 부른다. */
  const onError = () => {
    if (retried.current && Date.now() - mutation.submittedAt < STALE_AFTER_MS) {
      setPlayFailed(true);
      return;
    }
    retried.current = true;
    mutation.mutate();
  };

  const url = mutation.isError || playFailed ? undefined : mutation.data?.url;
  return {
    url,
    /** 영상 요소의 key. 주소가 바뀌거나 재생을 다시 누르면 영상을 새로 띄운다. */
    videoKey: `${url}#${round}`,
    pending: mutation.isPending,
    error: mutation.isError
      ? '재생 주소를 받지 못했습니다'
      : playFailed
        ? '재생하지 못했습니다'
        : undefined,
    play,
    onError,
  };
}
