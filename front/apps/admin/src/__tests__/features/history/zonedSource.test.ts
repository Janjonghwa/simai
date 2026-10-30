import { replayFiles } from '@/features/history/replay';
import { replayResponseSchema } from '@/features/history/types';
import { withZones } from '@/features/history/zonedSource';
import type { ReplaySpatialSource, SpatialSourceSink } from '@/features/viewer3d';
import { parseReplayLog } from '@/features/viewer3d/replay/replayLog';
import { createReplaySource } from '@/features/viewer3d/replay/replaySource';
import { historyReplay, historyWorks } from '@/mocks/history';
import { describe, expect, it } from 'vitest';

const T = 1_760_342_400_000;
const ZONES = [{ zone_id: 'z-02' }];
const PUSHED = `zones:${JSON.stringify(ZONES)}@0`;

/** 받은 순서를 글로 남기는 구독자. */
function recorder() {
  const log: string[] = [];
  const sink: SpatialSourceSink = {
    onMessage: () => log.push('message'),
    onConnectionChange: (change) => log.push(`connection:${change.state}`),
    onReset: () => log.push('reset'),
    onZones: (result, at) =>
      log.push(result.ok ? `zones:${JSON.stringify(result.raw)}@${at - T}` : 'zones:fail'),
  };
  return { log, sink };
}

/** 구독자를 손으로 부르는 재생 Source. 구독하면 진짜처럼 먼저 비운다(onReset). */
function fakeReplay() {
  let current: SpatialSourceSink | null = null;
  const replay = {
    id: 'replay-1',
    kind: 'REPLAY',
    subscribe(sink: SpatialSourceSink) {
      current = sink;
      sink.onReset?.();
      return () => {
        current = null;
      };
    },
  } as unknown as ReplaySpatialSource;
  return { replay, sink: () => current };
}

describe('구역을 함께 보내는 재생 Source(withZones)', () => {
  it('재생 Source 의 id·출처를 그대로 쓰고, 메시지·연결 변화를 그대로 넘긴다', () => {
    const { replay, sink } = fakeReplay();
    const zoned = withZones(replay, () => T);
    expect([zoned.id, zoned.kind]).toEqual(['replay-1', 'REPLAY']);

    const got = recorder();
    zoned.subscribe(got.sink);
    sink()!.onMessage({ a: 1 }, T);
    sink()!.onConnectionChange({ state: 'OPEN', at: T });
    expect(got.log).toEqual(['reset', 'message', 'connection:OPEN']);
  });

  it('구역을 받기 전에는 보내지 않고, 받으면 구독자 모두에게, 나중에 구독해도 보낸다', () => {
    const { replay } = fakeReplay();
    const zoned = withZones(replay, () => T);
    const a = recorder();
    const b = recorder();
    zoned.subscribe(a.sink);
    zoned.subscribe(b.sink);
    expect(a.log).toEqual(['reset']);

    zoned.setZones(ZONES);
    expect(a.log).toEqual(['reset', PUSHED]);
    expect(b.log).toEqual(['reset', PUSHED]);

    const late = recorder();
    zoned.subscribe(late.sink);
    expect(late.log.at(-1)).toBe(PUSHED);
  });

  it('되감기로 비울 때마다 구역을 다시 넣고, 구독을 끊으면 더 보내지 않는다', () => {
    const { replay, sink } = fakeReplay();
    const zoned = withZones(replay, () => T);
    zoned.setZones(ZONES);
    const got = recorder();
    const off = zoned.subscribe(got.sink);
    got.log.length = 0;

    sink()!.onReset!();
    expect(got.log).toEqual(['reset', PUSHED]);

    off();
    got.log.length = 0;
    zoned.setZones([]);
    expect(sink()).toBeNull();
    expect(got.log).toEqual([]);
  });

  it('진짜 재생 엔진에서 뒤로 옮기면 비운 뒤 구역을 다시 받고, 그 시각 기록을 받는다', () => {
    const now = new Date(2026, 9, 29, 11, 0).getTime();
    const works = historyWorks(now);
    const from = works.find((w) => w.date === '2026-10-28')!.from_ms;
    // 서버가 주는 모양(봉투)으로 읽는다: 이벤트는 LIVE 로 오고 replayFiles 가 REPLAY 로 바꾼다.
    const data = replayResponseSchema.parse(historyReplay(works, from, from + 60_000, 500, now));
    const log = parseReplayLog(replayFiles(data));
    expect(log.issues).toEqual([]);
    const replay = createReplaySource(log, {
      autoplay: false,
      gapMs: 1_500,
    });
    const zoned = withZones(replay, () => T);
    zoned.setZones(ZONES);
    const got = recorder();
    zoned.subscribe(got.sink);

    replay.seek(from + 50_000);
    got.log.length = 0;
    replay.seek(from + 10_000);
    expect(got.log.slice(0, 2)).toEqual(['reset', PUSHED]);
    expect(got.log).toContain('message');
  });
});
