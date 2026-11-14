import { GPU_ERROR } from '@/features/gpu/api';
import type { GpuStatus, GpuStock } from '@/features/gpu/types';
import { ApiError } from '@/lib/http';

// 상단바 GPU 점·정보창·패널이 쓰는 순수 계산(S15P31A101-702). 서버 상태(GET /api/gpu/sessions/current 의 power·launch·session)를
// 점 네 가지로 줄이고, 정보창 줄(종류·지역, 경과·예상 비용, 준비 단계)을 만든다.

/** 상단바 점: 초록 연결 / 노랑(깜빡임) 준비 중 / 회색 꺼짐 / 빨강 오류 */
export type GpuDot = 'CONNECTED' | 'PREPARING' | 'OFF' | 'ERROR';

export const DOT_LABEL: Record<GpuDot, string> = {
  CONNECTED: 'GPU 연결',
  PREPARING: 'GPU 준비 중',
  OFF: 'GPU 꺼짐',
  ERROR: 'GPU 오류',
};

/** 준비 단계 이름. 0 은 공급자가 인스턴스를 띄우는 중(G1 등록 전) */
export const PREP_STEPS = ['켜는 중', '가중치', '엔진', '준비 완료'] as const;

/**
 * 점 상태. 관리자가 아니라 상태를 못 받았으면(status 없음) Tier 0 연결만으로 연결·꺼짐을 가른다.
 * 서버가 power 를 싣지 않았으면(-702 이전 서버) 세션으로 고른다.
 */
export function gpuDot(status: GpuStatus | undefined, tier0Connected: boolean): GpuDot {
  if (!status) return tier0Connected ? 'CONNECTED' : 'OFF';
  switch (status.power) {
    case 'CONNECTED':
      return 'CONNECTED';
    case 'STARTING':
    case 'PREPARING':
      return 'PREPARING';
    case 'ERROR':
      return 'ERROR';
    case 'OFF':
      return 'OFF';
    default: {
      const s = status.session;
      if (!s || s.state !== 'ACTIVE') return 'OFF';
      if (s.stage === 'FAILED') return 'ERROR';
      return s.stage === 'READY' || s.tier0_link === 'CONNECTED' ? 'CONNECTED' : 'PREPARING';
    }
  }
}

/** 준비 단계 번호(PREP_STEPS). 준비 중이 아니면 null */
export function prepStep(status: GpuStatus | undefined): number | null {
  if (!status) return null;
  if (status.power === 'STARTING') return 0;
  if (status.power !== 'PREPARING') return null;
  const stage = status.session?.stage;
  if (stage === 'ENGINE' || stage === 'TIER0' || stage === 'VLLM') return 2;
  if (stage === 'READY') return 3;
  return 1;
}

/** 켜져 있거나 켜는 중이라 끄기 대상이 있는지(없으면 패널이 켜기를 보인다) */
export function isGpuOn(status: GpuStatus | undefined): boolean {
  if (!status) return false;
  const launch = status.launch;
  if (launch && (launch.state === 'REQUESTED' || launch.state === 'RUNNING')) return true;
  return status.session?.state === 'ACTIVE';
}

export interface GpuSummary {
  gpuType: string;
  region: string | null;
  /** 켠 뒤 지난 시간(ms). 켠 요청이 열려 있으면 요청부터, 아니면 세션 등록부터 */
  elapsedMs: number;
  pricePerHourUsd: number | null;
  /** 시간당 가격 × 경과. 가격을 모르면 null */
  costUsd: number | null;
}

/** 정보창 요약. 켜져 있지 않으면 null. nowMs 는 서버 시계로 맞춘 지금 */
export function gpuSummary(status: GpuStatus | undefined, nowMs: number): GpuSummary | null {
  if (!status || !isGpuOn(status)) return null;
  const session = status.session?.state === 'ACTIVE' ? status.session : null;
  const launch =
    status.launch && (status.launch.state === 'REQUESTED' || status.launch.state === 'RUNNING')
      ? status.launch
      : null;
  const started = launch?.requested_at_ms ?? session?.registered_at_ms ?? nowMs;
  const elapsedMs = Math.max(0, nowMs - started);
  const price = session?.price_per_hour_usd ?? launch?.price_per_hour_usd ?? null;
  return {
    gpuType: session?.gpu_type ?? launch?.gpu_type ?? '',
    region: session?.region ?? launch?.region ?? null,
    elapsedMs,
    pricePerHourUsd: price,
    costUsd: price === null ? null : (price * elapsedMs) / 3_600_000,
  };
}

/** 오류 점일 때 한 줄 까닭 */
export function gpuErrorReason(status: GpuStatus | undefined): string | null {
  if (!status || status.power !== 'ERROR') return null;
  const session = status.session;
  if (session?.state === 'ACTIVE' && session.stage === 'FAILED') {
    return `준비 실패${session.stage_reason_code ? ` (${session.stage_reason_code})` : ''}`;
  }
  const launch = status.launch;
  if (launch?.state === 'FAILED') {
    return launch.failure_code === 'OUT_OF_STOCK' ? '켜기 실패 (재고 없음)' : '켜기 실패';
  }
  if (launch?.state === 'RUNNING' && session?.state === 'ENDED') return 'GPU 연결이 끊겼습니다';
  return '끄기 실패 · 다시 시도하세요';
}

export const STOP_BLOCK_TEXT: Record<'LIVE' | 'SETUP', string> = {
  LIVE: '관제(작업) 중에는 끌 수 없습니다',
  SETUP: '세팅 중에는 끌 수 없습니다',
};

export const STOCK_LABEL: Record<GpuStock, string> = {
  HIGH: '많음',
  MEDIUM: '보통',
  LOW: '적음',
  NONE: '없음',
  UNKNOWN: '모름',
};

/** "1시간 5분", "12분", "1분 미만" */
export function formatElapsed(ms: number): string {
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return '1분 미만';
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${rest}분`;
  return rest === 0 ? `${hours}시간` : `${hours}시간 ${rest}분`;
}

export function formatUsd(value: number): string {
  return `$${value.toFixed(2)}`;
}

/** 켜기·끄기·설정 저장 실패를 안내 문구로 */
export function gpuErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.code) {
      case GPU_ERROR.ALREADY_ON:
        return 'GPU 가 이미 켜져 있거나 켜는 중입니다.';
      case GPU_ERROR.NOT_ON:
        return '켜져 있는 GPU 가 없습니다.';
      case GPU_ERROR.OUT_OF_STOCK:
        return '고른 GPU·지역에 재고가 없습니다. 다른 칸을 고르세요.';
      case GPU_ERROR.PRICE_OVER_CAP:
        return '시간당 가격이 GPU 설정의 상한보다 높습니다.';
      case GPU_ERROR.STOP_BLOCKED_LIVE:
        return `${STOP_BLOCK_TEXT.LIVE}. 작업을 먼저 종료하세요.`;
      case GPU_ERROR.STOP_BLOCKED_SETUP:
        return `${STOP_BLOCK_TEXT.SETUP}. 세팅을 끝내거나 취소하세요.`;
      case GPU_ERROR.STOP_UNSUPPORTED:
        return '손으로 켠 GPU 는 여기서 끌 수 없습니다.';
      case GPU_ERROR.NOT_CONFIGURED:
        return 'GPU 공급자 설정(API 키 또는 템플릿 ID)이 없습니다. GPU 설정을 확인하세요.';
      case GPU_ERROR.INVALID_SETTINGS:
        return '입력 값을 확인하세요.';
      case GPU_ERROR.PROVIDER_ERROR:
        return 'GPU 공급자 요청이 실패했습니다. 잠시 뒤 다시 시도하세요.';
    }
    if (error.status === 403) return '관리자만 할 수 있습니다.';
  }
  return '요청이 실패했습니다. 잠시 뒤 다시 시도하세요.';
}
