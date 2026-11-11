import { useEffect, useRef, useState } from 'react';

// 세팅 진행 막대를 시간으로 부드럽게 채운다. 서버 워커는 단계가 끝날 때만 진행률을 올려서(배경 수집 중에는 0~14 연속값,
// 그 뒤로는 BEATS 의 값) 그대로 그리면 막대가 오래 멈췄다가 한 번에 뛴다. 서버 값 사이는 다음 값을 향해 천천히 차오르게 하되
// 다음 값에 닿기 전에 멈춰서 서버보다 앞서 완료로 보이지 않게 한다.

/** 서버 워커가 진행률을 올리는 값. 15 미만은 배경 수집 중이라 서버가 연속값을 준다. */
export const PROGRESS_BEATS = [15, 40, 65, 80, 85, 95, 100] as const;

/** 막대가 서버 값 사이 간격에서 채울 수 있는 비율의 상한(다음 서버 값 직전에서 멈춘다). */
export const SMOOTH_MAX_FRACTION = 0.9;

/** 다음 서버 값까지 차오르는 속도 상수(ms, 클수록 천천히). 15 초로 시작했고 실제로 돌려 본 뒤 조정한다. */
export const SMOOTH_TAU_MS = 15_000;

/** last 다음으로 서버가 올릴 값. 이미 끝이면 null. */
export function nextBeatAfter(last: number): number | null {
  return PROGRESS_BEATS.find((beat) => beat > last) ?? null;
}

/**
 * last 에서 nextBeat 로 향하는 표시값. elapsedMs 가 지날수록 간격을 1 − e^(−t/τ) 비율로 채우되 간격의
 * SMOOTH_MAX_FRACTION 을 넘지 않는다.
 */
export function smoothProgress({
  last,
  nextBeat,
  elapsedMs,
  tauMs,
}: {
  last: number;
  nextBeat: number;
  elapsedMs: number;
  tauMs: number;
}): number {
  const gap = nextBeat - last;
  if (gap <= 0 || tauMs <= 0) return last;
  const filled = 1 - Math.exp(-Math.max(0, elapsedMs) / tauMs);
  return last + gap * Math.min(filled, SMOOTH_MAX_FRACTION);
}

/** 서버 값 server 를 받은 지 elapsedMs 가 지났을 때의 표시값. 배경 수집 구간(15 미만)은 서버 값 그대로다. */
function displayAt(server: number, elapsedMs: number): number {
  if (server < PROGRESS_BEATS[0]) return server;
  const nextBeat = nextBeatAfter(server);
  if (nextBeat === null) return server;
  return smoothProgress({ last: server, nextBeat, elapsedMs, tauMs: SMOOTH_TAU_MS });
}

/** 표시값이 서버 값 server 에서 아직 더 오를 수 있는지. 다음 서버 값 직전(시간이 무한히 지난 값)에 닿았거나 올릴 목표가 없으면 false. */
function canRise(track: ProgressTrack): boolean {
  return track.shown < displayAt(track.server, Infinity);
}

/** 마지막 서버 값과 그것을 받은 시각, 지금 보이는 값. */
export interface ProgressTrack {
  server: number;
  since: number;
  shown: number;
}

export function startTrack(serverPct: number, nowMs: number): ProgressTrack {
  return { server: serverPct, since: nowMs, shown: serverPct };
}

/**
 * 한 시점의 표시값을 계산한다. 서버 값이 새로 오면 그 값으로 바로 바뀌고, 표시값은 서버 값이 내려갈 때(자동 재시도)만
 * 내려간다(그 밖에는 단조 증가). running 이 false(실패)면 새 서버 값만 받아들이고 더는 차오르지 않는다.
 */
export function advanceProgress(
  track: ProgressTrack,
  serverPct: number,
  nowMs: number,
  running = true,
): ProgressTrack {
  if (serverPct < track.server) return startTrack(serverPct, nowMs);
  const base =
    serverPct > track.server
      ? { server: serverPct, since: nowMs, shown: Math.max(track.shown, serverPct) }
      : track;
  if (!running) return base;
  return { ...base, shown: Math.max(base.shown, displayAt(base.server, nowMs - base.since)) };
}

/**
 * 서버 진행률(serverPct)을 시간으로 부드럽게 채운 표시값(0.1 단위). running 이 false 면 멈춘다.
 * 표시값이 더 오를 수 없게 되면(다음 서버 값 직전·100) 프레임 요청을 멈추고, 서버 값이 바뀌면 다시 돈다.
 */
export function useSmoothProgress(serverPct: number, running = true): number {
  const [shown, setShown] = useState(serverPct);
  const trackRef = useRef<ProgressTrack | null>(null);

  useEffect(() => {
    const tick = () => {
      const now = performance.now();
      const track = advanceProgress(
        trackRef.current ?? startTrack(serverPct, now),
        serverPct,
        now,
        running,
      );
      trackRef.current = track;
      setShown(Math.round(track.shown * 10) / 10);
      if (running && canRise(track)) frame = requestAnimationFrame(tick);
    };
    let frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [serverPct, running]);

  return shown;
}
