import {
  cameraRows,
  connectionRows,
  latencyRows,
  linkLabel,
  processingRows,
} from '@/features/system/health';
import type { MetricsResponse } from '@/features/system/types';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import type { HealthSnapshot } from '@/store/realtime.store';
import type { ViewerHealthMessage, ViewerHealthPayload } from '@simai/contracts';
import { describe, expect, it } from 'vitest';

// T 는 mock 바퀴(40초)의 0초라 카메라 4대가 정상이다. T + 20초부터 6초 동안 cam3 가 키프레임을 기다린다.
const T = 1_760_342_400_000;

function snapshot(t = T, change: Partial<ViewerHealthPayload> = {}): HealthSnapshot {
  return { payload: { ...viewerHealthAt(t).payload, ...change }, receivedAt: t, clockOffsetMs: 0 };
}

// 저장소 루트 contracts/ 의 정본 예제. 손으로 옮겨 적지 않고 그대로 읽는다.
const EXAMPLES = import.meta.glob<{ default: ViewerHealthMessage }>(
  '../../../../../../../contracts/examples/viewer_health*.json',
  { eager: true },
);

function example(file: string): HealthSnapshot {
  const found = Object.entries(EXAMPLES).find(([path]) => path.endsWith(`/${file}.json`));
  if (!found) throw new Error(`계약 예제 ${file} 가 없다`);
  return { payload: structuredClone(found[1].default).payload, receivedAt: T, clockOffsetMs: 0 };
}

const tier0Lost = (t = T) =>
  snapshot(t, { tier0_link: 'DISCONNECTED', system_health: 'UNSAFE_UNKNOWN' });

const metrics: MetricsResponse = {
  contract: 'DEMO_UNCONFIRMED',
  as_of: T,
  stages: {
    capture_to_site_alert_ms: { p50: 340, p95: 520.4 },
    capture_to_remote_delivery_ms: {
      viewer: { p50: 620, p95: null },
      pwa: { p50: 1400, p95: 2300 },
    },
  },
};

describe('cameraRows', () => {
  it('카메라별 상태·FPS·시각 차이·손실 3종·큐 대기를 단위와 함께 만든다', () => {
    const rows = cameraRows(snapshot());
    expect(rows.map((row) => row.id)).toEqual(['cam1', 'cam2', 'cam3', 'cam4']);
    expect(rows[0]).toEqual({
      id: 'cam1',
      status: '정상',
      ok: true,
      fps: '14.6',
      syncOffset: '4 ms',
      gapMax: '70 ms',
      runpodLoss: '0',
      skipped: '10',
      queueWaitP95: '36 ms',
    });
    expect(rows[1]).toMatchObject({
      fps: '14.9',
      gapMax: '100 ms',
      runpodLoss: '1',
      queueWaitP95: '48 ms',
    });
  });

  it('계약 예제를 칸마다 그대로 옮긴다', () => {
    const [cam1, cam2] = cameraRows(example('viewer_health'));
    expect(cam1).toEqual({
      id: 'cam1',
      status: '정상',
      ok: true,
      fps: '14.8',
      syncOffset: '6 ms',
      gapMax: '80 ms',
      runpodLoss: '0',
      skipped: '10',
      queueWaitP95: '40 ms',
    });
    expect(cam2).toEqual({ id: 'cam2', status: '키프레임 대기', ok: false, fps: '0.0' });
  });

  it('키프레임을 기다리는 카메라는 처리 FPS 0 을 그대로 보이고, 잴 수 없는 칸은 비운다', () => {
    const cam3 = cameraRows(snapshot(T + 20_000)).find((row) => row.id === 'cam3');
    expect(cam3).toEqual({
      id: 'cam3',
      status: '키프레임 대기',
      ok: false,
      fps: '0.0',
      syncOffset: undefined,
      gapMax: undefined,
      runpodLoss: undefined,
      skipped: undefined,
      queueWaitP95: undefined,
    });
  });

  it('실제 0 은 0 으로 보인다', () => {
    const [camera] = cameraRows(
      snapshot(T, {
        tier0_health: {
          cameras: [
            {
              id: 'c',
              status: 'OK',
              fps: 15,
              sync_offset_ms: 0,
              queue_wait_ms: { p50: 0, p95: 0 },
              frames: {
                camera_to_gateway_gap_max_ms: 0,
                gateway_to_runpod_loss: 0,
                skipped_by_policy: 0,
              },
            },
          ],
        },
      }),
    );
    expect(camera).toMatchObject({
      fps: '15.0',
      syncOffset: '0 ms',
      gapMax: '0 ms',
      runpodLoss: '0',
      skipped: '0',
      queueWaitP95: '0 ms',
    });
  });

  it.each([
    ['OK', '정상'],
    ['NO_SIGNAL', '신호 없음'],
    ['WAITING_KEYFRAME', '키프레임 대기'],
    ['SYNC_INVALID', '동기 불가'],
  ])('상태 %s 는 "%s" 다', (status, label) => {
    const [camera] = cameraRows(snapshot(T, { tier0_health: { cameras: [{ id: 'c', status }] } }));
    expect(camera).toMatchObject({ status: label, ok: status === 'OK' });
  });

  it('형식이 틀린 칸만 비우고, 모르는 상태는 그대로 보이고, id 가 없는 카메라만 뺀다', () => {
    const health = snapshot(T, {
      tier0_health: {
        cameras: [
          {
            id: 'cam1',
            status: 'REBOOTING',
            fps: '15',
            sync_offset_ms: 4,
            frames: { gateway_to_runpod_loss: 'x', skipped_by_policy: 3 },
          },
          { id: 'cam2', status: 7, queue_wait_ms: 'slow', frames: 'x' },
          { id: 3, status: 'OK' },
          'x',
        ],
      },
    });
    expect(cameraRows(health)).toEqual([
      { id: 'cam1', status: 'REBOOTING', ok: false, syncOffset: '4 ms', skipped: '3' },
      { id: 'cam2', ok: false },
    ]);
  });

  it('health·tier0_health 가 없거나 cameras 가 배열이 아니면 카메라가 없다', () => {
    expect(cameraRows(null)).toEqual([]);
    expect(cameraRows(snapshot(T, { tier0_health: null }))).toEqual([]);
    expect(cameraRows(snapshot(T, { tier0_health: { cameras: 'x' } }))).toEqual([]);
  });

  it('EC2 ↔ Tier 0 가 끊기면 마지막으로 받은 카메라를 지금 값처럼 보이지 않는다', () => {
    expect(cameraRows(tier0Lost())).toEqual([]);
  });
});

describe('latencyRows', () => {
  it('촬영 → 판정은 health 에서, 현장 경보·원격 전달(관제 화면·반장 PWA)은 지표 API 에서 받고 없는 값은 비운다', () => {
    expect(latencyRows(snapshot(), metrics)).toEqual([
      { key: 'risk', label: '촬영 → 판정', p50: '300 ms', p95: '460 ms' },
      { key: 'site', label: '촬영 → 현장 소리', p50: '340 ms', p95: '520 ms' },
      { key: 'viewer', label: '촬영 → 관제 화면', p50: '620 ms', p95: undefined },
      { key: 'pwa', label: '촬영 → 반장 PWA', p50: '1400 ms', p95: '2300 ms' },
    ]);
  });

  it('계약 예제의 촬영 → 판정을 보이고, ms 는 반올림한다', () => {
    expect(latencyRows(example('viewer_health'), undefined)[0]).toMatchObject({
      p50: '310 ms',
      p95: '480 ms',
    });
    const health = snapshot(T, {
      tier0_health: { capture_to_risk_ms: { p50: 299.5, p95: 520.4 } },
    });
    expect(latencyRows(health, undefined)[0]).toMatchObject({ p50: '300 ms', p95: '520 ms' });
  });

  it('지표 응답에 구간이나 값이 빠지면 그 칸만 비운다', () => {
    const values = (stages: MetricsResponse['stages']) =>
      latencyRows(null, { contract: 'DEMO_UNCONFIRMED', as_of: T, stages }).map(({ p50, p95 }) => [
        p50,
        p95,
      ]);
    expect(values({ capture_to_site_alert_ms: { p50: 1 } })).toEqual([
      [undefined, undefined],
      ['1 ms', undefined],
      [undefined, undefined],
      [undefined, undefined],
    ]);
    // 원격 전달은 목적지마다 따로 잰다. 한쪽만 와도 그 칸만 채운다.
    expect(values({ capture_to_remote_delivery_ms: { pwa: { p95: 2400 } } })).toEqual([
      [undefined, undefined],
      [undefined, undefined],
      [undefined, undefined],
      [undefined, '2400 ms'],
    ]);
  });

  it('지표가 있어도 health 가 없거나 Tier 0 가 끊겼으면 촬영 → 판정을 비운다', () => {
    for (const health of [null, snapshot(T, { tier0_health: null }), tier0Lost()]) {
      expect(latencyRows(health, metrics)[0]).toMatchObject({ p50: undefined, p95: undefined });
      expect(latencyRows(health, metrics)[1]).toMatchObject({ p50: '340 ms' });
    }
  });
});

describe('processingRows', () => {
  it('파이프라인 지연(지연 표시)·GPU 사용률·GPU 메모리를 보인다', () => {
    expect(processingRows(snapshot())).toEqual([
      { key: 'lag', label: '파이프라인 지연', value: '110 ms', delay: true },
      { key: 'gpu', label: 'GPU 사용률', value: '68%' },
      { key: 'gpu-memory', label: 'GPU 메모리', value: '62%' },
    ]);
    expect(processingRows(example('viewer_health')).map((row) => row.value)).toEqual([
      '120 ms',
      '71%',
      '62%',
    ]);
  });

  it('실제 0 은 0 으로, 비율은 반올림해 보인다', () => {
    const zero = snapshot(T, {
      tier0_health: { pipeline_lag_ms: 0, gpu_util: 0, gpu_memory_used_ratio: 0.686 },
    });
    expect(processingRows(zero).map((row) => row.value)).toEqual(['0 ms', '0%', '69%']);
  });

  it('파이프라인 지연을 모르면(null) 하한을 대신 보이지 않고 비운다(계약 2.0.0)', () => {
    const health = snapshot(T, {
      tier0_health: { pipeline_lag_ms: null, pipeline_lag_lower_bound_ms: 900, gpu_util: 0.5 },
    });
    expect(processingRows(health).map((row) => row.value)).toEqual([undefined, '50%', undefined]);
  });

  it('형식이 틀린 칸 하나는 그 칸만 비우고 카메라·다른 값은 그대로 그린다', () => {
    const tier0 = viewerHealthAt(T).payload.tier0_health as Record<string, unknown>;
    const health = snapshot(T, { tier0_health: { ...tier0, gpu_util: 'N/A' } });
    expect(processingRows(health).map((row) => row.value)).toEqual(['110 ms', undefined, '62%']);
    expect(cameraRows(health)).toHaveLength(4);
  });

  it('값이 없거나 Tier 0 가 끊겼으면 비운다', () => {
    for (const health of [snapshot(T, { tier0_health: null }), tier0Lost(), null]) {
      expect(processingRows(health).map((row) => row.value)).toEqual([
        undefined,
        undefined,
        undefined,
      ]);
    }
  });
});

describe('connectionRows', () => {
  const values = (health: HealthSnapshot | null) => connectionRows(health).map((row) => row.value);

  it('노트북 ↔ Tier 0 는 gateway_link, 노트북 ↔ EC2 는 laptop.link, Tier 0 ↔ EC2 는 tier0_link 다', () => {
    expect(connectionRows(snapshot()).map((row) => row.label)).toEqual([
      '노트북 ↔ Tier 0',
      '노트북 ↔ EC2',
      'Tier 0 ↔ EC2',
    ]);
    expect(values(snapshot())).toEqual(['연결됨', '연결됨', '연결됨']);
    const laptopLost = snapshot(T, {
      laptop: { link: 'DISCONNECTED', last_heartbeat_at: T - 5000 },
      system_health: 'UNSAFE_UNKNOWN',
    });
    expect(values(laptopLost)).toEqual(['연결됨', '끊김', '연결됨']);
    const tier0 = viewerHealthAt(T).payload.tier0_health as Record<string, unknown>;
    expect(
      values(snapshot(T, { tier0_health: { ...tier0, gateway_link: 'DISCONNECTED' } })),
    ).toEqual(['끊김', '연결됨', '연결됨']);
  });

  it('Tier 0 가 끊기면 Tier 0 가 알린 노트북 연결은 지금 값이 아니라 비운다', () => {
    expect(values(tier0Lost())).toEqual([undefined, '연결됨', '끊김']);
    expect(values(example('viewer_health.remote_link_lost'))).toEqual([
      undefined,
      '연결됨',
      '끊김',
    ]);
    expect(values(null)).toEqual([undefined, undefined, undefined]);
  });
});

describe('linkLabel', () => {
  it('연결 상태 문구를 만들고, 모르는 값은 그대로, 없으면 비운다', () => {
    expect(linkLabel('CONNECTED')).toBe('연결됨');
    expect(linkLabel('DISCONNECTED')).toBe('끊김');
    expect(linkLabel('DEGRADED')).toBe('DEGRADED');
    expect(linkLabel(null)).toBeUndefined();
  });
});
