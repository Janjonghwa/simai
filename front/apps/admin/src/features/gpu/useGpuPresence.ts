import { requestGpuPresence } from '@/features/gpu/api';
import { api } from '@/lib/api';
import { useMutation } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';

// 관리자 화면 조작 신호(S15P31A101-708). 화면이 보이는 동안(document.visibilityState=visible) 사용자 입력(누르기·키) 뒤
// 최대 1분에 한 번 POST /api/gpu/presence 를 보낸다. 서버는 Tier 0 가 붙어 있을 때 이것을 GPU 사용으로 세어 자동 정지를 미룬다.
// 상태 주기 조회(10초)는 화면을 띄워 두기만 해도 오므로 신호로 쓰지 않는다. 실패는 알리지 않는다(다음 입력 때 다시 보낸다).

/** 신호 사이 최소 간격. 서버 자동 정지 점검 주기(1분)와 같다 */
export const GPU_PRESENCE_MIN_INTERVAL_MS = 60_000;

const INPUT_EVENTS = ['pointerdown', 'keydown'] as const;

export function useGpuPresence(enabled: boolean): void {
  const { mutate } = useMutation({
    mutationFn: () => requestGpuPresence(api),
  });
  const lastSentRef = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const onInput = () => {
      if (document.visibilityState !== 'visible') return;
      const now = Date.now();
      const last = lastSentRef.current;
      if (last !== null && now - last < GPU_PRESENCE_MIN_INTERVAL_MS) return;
      lastSentRef.current = now;
      mutate();
    };
    for (const type of INPUT_EVENTS)
      window.addEventListener(type, onInput, { capture: true, passive: true });
    return () => {
      for (const type of INPUT_EVENTS) window.removeEventListener(type, onInput, { capture: true });
    };
  }, [enabled, mutate]);
}
