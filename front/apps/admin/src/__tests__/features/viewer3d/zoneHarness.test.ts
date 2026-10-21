import type { ZonesResult } from '@/features/viewer3d/data/spatialSource';
import { readZoneApply, withZoneApply } from '@/features/viewer3d/dev-harness/harnessSource';
import { createMockSource, NOT_JSON_REASON } from '@/features/viewer3d/dev-harness/mockSource';
import { displayName, LEGACY_ROSTER } from '@/features/viewer3d/dev-harness/mockUi';
import { DEMO_ZONES_RESPONSE } from '@/features/viewer3d/dev-harness/scenarios';
import { afterEach, describe, expect, it, vi } from 'vitest';

const T0 = 1_760_342_400_000;

describe('개발 화면 구역 표시 주소', () => {
  it.each([
    ['', 'UNVERIFIED'],
    ['?zones=preview', 'DEMO_PREVIEW'],
    ['?zones=APPLIED', 'DEMO_ASSUMED_APPLIED'],
    ['?zones=active', 'UNVERIFIED'],
  ])('%s → %s', (search, state) => {
    expect(readZoneApply(search)).toBe(state);
  });

  it('바꾼 주소는 다른 매개변수를 두고 기본값이면 zones 를 지운다', () => {
    const base = 'http://127.0.0.1:5179/x/index.html?view=combined&source=normal';
    expect(withZoneApply(base, 'DEMO_PREVIEW')).toBe(`${base}&zones=preview`);
    expect(withZoneApply(`${base}&zones=preview`, 'UNVERIFIED')).toBe(base);
  });

  it('레거시 구역은 이름에 레거시를 밝히고, Provider 구역은 ID 로 부른다', () => {
    expect(LEGACY_ROSTER.zones.every((z) => z.legacy && z.name.includes('레거시'))).toBe(true);
    expect(displayName({ type: 'zone', id: 'zoneA' })).toBe('레거시 예시 구역 A');
    expect(displayName({ type: 'zone', id: 'z-01' })).toBe('구역 z-01');
  });
});

describe('MockSource 구역 REST', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const html = () =>
    new Response('<!doctype html><html></html>', { headers: { 'content-type': 'text/html' } });

  function start(responses: (() => Response)[]) {
    const queue = [...responses];
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        calls.push(url);
        if (url.includes('/api/zones')) return queue.shift()!();
        return new Response(null, { status: 404 });
      }),
    );
    const zones: ZonesResult[] = [];
    const source = createMockSource({ scenario: 'normal', t0: T0, restBase: '', now: () => T0 });
    const off = source.subscribe({
      onMessage: () => {},
      onConnectionChange: () => {},
      onZones: (result) => zones.push(result),
    });
    source.pause();
    return { zones, calls: () => calls.filter((u) => u.includes('/api/zones')), off };
  }

  it('MSW 가 켜지기 전 HTML 응답은 JSON 으로 읽지 않고 잠시 뒤 다시 묻는다', async () => {
    vi.useFakeTimers();
    const run = start([html, html, () => Response.json(DEMO_ZONES_RESPONSE)]);
    await vi.advanceTimersByTimeAsync(1000);
    expect(run.calls()).toHaveLength(3);
    expect(run.zones).toEqual([{ ok: true, raw: DEMO_ZONES_RESPONSE }]);
    run.off();
  });

  it('계속 HTML 이면 이유를 남기고 거부한다', async () => {
    vi.useFakeTimers();
    const run = start(Array.from({ length: 5 }, () => html));
    await vi.advanceTimersByTimeAsync(5000);
    expect(run.calls()).toHaveLength(5);
    expect(run.zones).toEqual([{ ok: false, reason: NOT_JSON_REASON }]);
    run.off();
  });

  it('HTTP 503 은 다시 묻지 않고 그대로 알린다', async () => {
    vi.useFakeTimers();
    const run = start([() => Response.json({ message: 'x' }, { status: 503 })]);
    await vi.advanceTimersByTimeAsync(5000);
    expect(run.calls()).toHaveLength(1);
    expect(run.zones).toEqual([{ ok: false, reason: 'HTTP 503' }]);
    run.off();
  });

  it('구독이 풀리면 다시 묻지 않는다', async () => {
    vi.useFakeTimers();
    const run = start(Array.from({ length: 5 }, () => html));
    await vi.advanceTimersByTimeAsync(0);
    run.off();
    await vi.advanceTimersByTimeAsync(5000);
    expect(run.calls()).toHaveLength(1);
  });
});
