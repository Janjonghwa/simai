import { RECEIVE_MS } from '@/features/tbm/constants';
import { useEffect, useState } from 'react';

/** 작업을 고르면 잠깐 회의록을 받는 중(흉내)으로 두었다가 그 작업 내용으로 양식을 채운다. */
export function useReceiving(taskId: string) {
  /** 회의록을 다 받은 작업. 지금 작업과 다르면 받는 중이다. */
  const [readyId, setReadyId] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setReadyId(taskId), RECEIVE_MS);
    return () => window.clearTimeout(timer);
  }, [taskId]);

  return { receiving: readyId !== taskId, restart: () => setReadyId(null) };
}
