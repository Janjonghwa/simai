import {
  formatElapsed,
  gpuDot,
  gpuErrorReason,
  gpuSummary,
  isGpuOn,
  prepStep,
} from '@/features/gpu/gpuStatus';
import type { GpuLaunch, GpuSession, GpuStatus } from '@/features/gpu/types';
import { describe, expect, it } from 'vitest';

const T = 1_793_000_000_000;

const session = (change: Partial<GpuSession> = {}): GpuSession => ({
  session_id: 's-1',
  state: 'ACTIVE',
  stage: 'WEIGHTS',
  stage_reason_code: null,
  provider: 'RUNPOD',
  instance_id: 'pod-1',
  gpu_type: 'NVIDIA H100 80GB HBM3',
  region: 'AP-JP-1',
  price_per_hour_usd: 3,
  registered_at_ms: T + 60_000,
  elapsed_ms: 0,
  tier0_link: 'DISCONNECTED',
  ...change,
});

const launch = (change: Partial<GpuLaunch> = {}): GpuLaunch => ({
  launch_id: 'l-1',
  state: 'RUNNING',
  provider: 'RUNPOD',
  gpu_type: 'NVIDIA H100 80GB HBM3',
  region: 'AP-JP-1',
  cloud_type: 'SECURE',
  interruptible: false,
  price_per_hour_usd: 3,
  requested_by: 'admin1',
  requested_at_ms: T,
  instance_id: 'pod-1',
  ended_at_ms: null,
  end_reason: null,
  failure_code: null,
  ...change,
});

const status = (change: Partial<GpuStatus>): GpuStatus => ({
  session: null,
  server_time_ms: T,
  launch: null,
  power: 'OFF',
  stop_block: null,
  ...change,
});

describe('gpuDot', () => {
  it('서버 power 를 점 네 가지로 줄인다(켜는 중·준비 중은 같은 노랑)', () => {
    expect(gpuDot(status({ power: 'OFF' }), false)).toBe('OFF');
    expect(gpuDot(status({ power: 'STARTING' }), false)).toBe('PREPARING');
    expect(gpuDot(status({ power: 'PREPARING' }), false)).toBe('PREPARING');
    expect(gpuDot(status({ power: 'CONNECTED' }), true)).toBe('CONNECTED');
    expect(gpuDot(status({ power: 'ERROR' }), false)).toBe('ERROR');
  });

  it('상태를 못 받으면(관리자 아님) Tier 0 연결만으로 가른다', () => {
    expect(gpuDot(undefined, true)).toBe('CONNECTED');
    expect(gpuDot(undefined, false)).toBe('OFF');
  });

  it('power 가 없는 옛 서버 응답은 세션으로 고른다', () => {
    expect(gpuDot(status({ power: undefined, session: session({ stage: 'READY' }) }), false)).toBe(
      'CONNECTED',
    );
    expect(gpuDot(status({ power: undefined, session: session({ stage: 'FAILED' }) }), false)).toBe(
      'ERROR',
    );
    expect(gpuDot(status({ power: undefined, session: session() }), false)).toBe('PREPARING');
    expect(gpuDot(status({ power: undefined, session: session({ state: 'ENDED' }) }), false)).toBe(
      'OFF',
    );
  });
});

describe('prepStep', () => {
  it('켜는 중 → 가중치 → 엔진(Tier 0·vLLM 포함) → 준비 완료', () => {
    expect(prepStep(status({ power: 'STARTING', launch: launch() }))).toBe(0);
    expect(
      prepStep(status({ power: 'PREPARING', session: session({ stage: 'REGISTERED' }) })),
    ).toBe(1);
    expect(prepStep(status({ power: 'PREPARING', session: session({ stage: 'WEIGHTS' }) }))).toBe(
      1,
    );
    for (const stage of ['ENGINE', 'TIER0', 'VLLM']) {
      expect(prepStep(status({ power: 'PREPARING', session: session({ stage }) }))).toBe(2);
    }
    expect(
      prepStep(status({ power: 'CONNECTED', session: session({ stage: 'READY' }) })),
    ).toBeNull();
  });
});

describe('gpuSummary', () => {
  it('켠 요청부터 경과를 재고 시간당 가격으로 예상 비용을 낸다', () => {
    const s = status({ power: 'PREPARING', launch: launch(), session: session() });
    const summary = gpuSummary(s, T + 30 * 60_000)!;
    expect(summary.gpuType).toBe('NVIDIA H100 80GB HBM3');
    expect(summary.region).toBe('AP-JP-1');
    expect(summary.elapsedMs).toBe(30 * 60_000);
    expect(summary.costUsd).toBeCloseTo(1.5);
  });

  it('켠 요청 없이 열린 세션(손으로 켬)은 등록부터 재고, 가격을 모르면 비용을 비운다', () => {
    const s = status({ power: 'CONNECTED', session: session({ price_per_hour_usd: null }) });
    const summary = gpuSummary(s, T + 61 * 60_000)!;
    expect(summary.elapsedMs).toBe(60 * 60_000);
    expect(summary.costUsd).toBeNull();
  });

  it('꺼져 있으면 요약이 없다', () => {
    expect(gpuSummary(status({ launch: launch({ state: 'STOPPED' }) }), T)).toBeNull();
    expect(isGpuOn(status({ launch: launch({ state: 'FAILED' }) }))).toBe(false);
    expect(isGpuOn(status({ launch: launch({ state: 'REQUESTED' }) }))).toBe(true);
  });
});

describe('gpuErrorReason', () => {
  it('준비 실패는 사유 코드, 켜기 실패는 재고 여부를 보인다', () => {
    expect(
      gpuErrorReason(
        status({
          power: 'ERROR',
          session: session({ stage: 'FAILED', stage_reason_code: 'ENGINE_BUILD_FAILED' }),
        }),
      ),
    ).toBe('준비 실패 (ENGINE_BUILD_FAILED)');
    expect(
      gpuErrorReason(
        status({
          power: 'ERROR',
          launch: launch({ state: 'FAILED', failure_code: 'OUT_OF_STOCK' }),
        }),
      ),
    ).toBe('켜기 실패 (재고 없음)');
    expect(gpuErrorReason(status({ power: 'OFF' }))).toBeNull();
  });
});

describe('formatElapsed', () => {
  it('분·시간으로 짧게', () => {
    expect(formatElapsed(30_000)).toBe('1분 미만');
    expect(formatElapsed(12 * 60_000)).toBe('12분');
    expect(formatElapsed(65 * 60_000)).toBe('1시간 5분');
    expect(formatElapsed(120 * 60_000)).toBe('2시간');
  });
});
