import { WALK_TARGET_SECONDS } from '@/features/setup/cameraCheck';
import type { CameraCheckPhase } from '@/features/setup/types';
import { useEffect, useState } from 'react';

const TICK_MS = 1_000;

/**
 * 걷기 창 카운트다운(S15P31A101-687). 시도가 걷는 중(WALKING)이 된 뒤 첫 박자부터 브라우저 시계로 30초를 센다(표시용, 판정은 워커가
 * 사람이 보인 시간으로 한다). 「확인만 다시」로 시도 번호가 바뀌면 다시 센다. 0 이 돼도 통과·실패가 올 때까지 창은 열려 있다.
 */
export function useWalkCountdown(phase: CameraCheckPhase | null, attemptNo: number): number {
  const walking = phase === 'WALKING';
  const [started, setStarted] = useState<{ attempt: number; at: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!walking) return;
    const timer = window.setInterval(() => {
      const t = Date.now();
      setNow(t);
      // 첫 박자에 이 시도의 걷기 시작 시각을 잡는다(그 전에는 WALK_TARGET_SECONDS 를 보인다)
      setStarted((prev) =>
        prev && prev.attempt === attemptNo ? prev : { attempt: attemptNo, at: t },
      );
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [walking, attemptNo]);
  if (!walking || started === null || started.attempt !== attemptNo) return WALK_TARGET_SECONDS;
  return Math.max(0, WALK_TARGET_SECONDS - Math.floor((now - started.at) / 1000));
}
