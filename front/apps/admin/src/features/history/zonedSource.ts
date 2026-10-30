import type { ReplaySpatialSource, SpatialSource, SpatialSourceSink } from '@/features/viewer3d';

/** 구역을 함께 보내는 재생 Source. */
export interface ZonedReplaySource extends SpatialSource {
  /** 3D 에 깔 구역(GET /api/zones 를 바꾼 상자 묶음). 바뀌면 구독자에게 다시 보낸다. */
  setZones(raw: unknown): void;
}

/**
 * 재생 Source(-187)는 World·이벤트만 보내고, 되감기(onReset)로 받는 쪽이 구역까지 비운다. 그래서 구역을 처음 구독할 때와
 * 되감기 뒤마다 다시 넣는다. 재생 Source 의 id 를 그대로 써서 Viewer 가 같은 스트림으로 본다.
 */
export function withZones(
  replay: ReplaySpatialSource,
  now: () => number = Date.now,
): ZonedReplaySource {
  let zones: unknown;
  const sinks = new Set<SpatialSourceSink>();
  const push = (sink: SpatialSourceSink) => {
    if (zones !== undefined) sink.onZones?.({ ok: true, raw: zones }, now());
  };
  return {
    id: replay.id,
    kind: 'REPLAY',
    subscribe(sink) {
      sinks.add(sink);
      const off = replay.subscribe({
        onMessage: (raw, receivedAt) => sink.onMessage(raw, receivedAt),
        onConnectionChange: (change) => sink.onConnectionChange(change),
        onReset: () => {
          sink.onReset?.();
          push(sink);
        },
      });
      push(sink);
      return () => {
        sinks.delete(sink);
        off();
      };
    },
    setZones(raw) {
      zones = raw;
      for (const sink of sinks) push(sink);
    },
  };
}
