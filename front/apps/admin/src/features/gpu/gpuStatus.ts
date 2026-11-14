import { GPU_ERROR } from '@/features/gpu/api';
import type { GpuOfferCell, GpuStatus, GpuTier } from '@/features/gpu/types';
import { ApiError } from '@/lib/http';

// 상단바 GPU 점·정보창·패널이 쓰는 순수 계산(S15P31A101-702). 서버 상태(GET /api/gpu/sessions/current 의 power·launch·session)를
// 점 네 가지로 줄이고, 정보창 줄(종류·지역, 경과·예상 비용, 준비 단계)을 만든다.

/** 상단바 점: 초록 연결 / 노랑(깜빡임) 준비 중 / 회색 꺼짐 / 빨강 오류 */
export type GpuDot = 'CONNECTED' | 'PREPARING' | 'OFF' | 'ERROR';

/** 점 상태 글. 상단바에는 「GPU」만 두고(-705) 이 글은 정보창 첫 줄과 화면 읽기 이름에 쓴다 */
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

/**
 * 칸 재고 글(S15P31A101-705): 1개 이상이면 되므로 등급(많음·보통·적음)은 쓰지 않는다. 켤 수 있음 / 없음 / 모름(AWS, 고를 수 있음).
 * 공급자가 개수를 주면 「n개」. 서버가 available 을 싣지 않았으면(-705 이전) 재고 등급에서 고른다.
 */
export function cellAvailable(cell: GpuOfferCell | undefined): boolean | null {
  if (!cell) return false;
  if (cell.available !== undefined) return cell.available;
  if (cell.stock === 'UNKNOWN') return null;
  return cell.stock !== 'NONE';
}

export function availabilityLabel(cell: GpuOfferCell | undefined): string {
  const available = cellAvailable(cell);
  if (available === null) return '모름';
  if (!available) return '없음';
  return cell?.count != null && cell.count > 0 ? `${cell.count}개` : '켤 수 있음';
}

/** 고를 수 있는 칸: 없음이 아니고(모름은 고를 수 있음) 가격 상한 안 */
export function cellUsable(cell: GpuOfferCell | undefined): boolean {
  return cell !== undefined && cellAvailable(cell) !== false && cell.within_cap;
}

/** VRAM 등급 글. 추천 등급(80GB급)은 따로 표시하지 않는다(추천 표시는 H100·H200 계열의 「추천」) */
export const TIER_LABEL: Record<Exclude<GpuTier, 'RECOMMENDED'>, string> = {
  BELOW_RECOMMENDED: '권장 미만',
  LIMITED: '제한',
  RISKY: '위험',
};

/**
 * VRAM 등급 색(사용자 결정 2026-11-14, front/CONVENTION.md 의 위험 등급 색 예외): 권장 미만 노랑 / 제한 주황 / 위험 빨강.
 * -704 카메라 지연 배지·-702 GPU 점과 같은 신호등 쓰임이다.
 */
export const TIER_TONE: Record<Exclude<GpuTier, 'RECOMMENDED'>, string> = {
  BELOW_RECOMMENDED: 'text-sev-caution ring-sev-caution/50',
  LIMITED: 'text-sev-high ring-sev-high/50',
  RISKY: 'text-sev-critical ring-sev-critical/50',
};

/** 확인창 경고 글 색(TIER_TONE 과 같은 색) */
export const TIER_TEXT: Record<Exclude<GpuTier, 'RECOMMENDED'>, string> = {
  BELOW_RECOMMENDED: 'text-sev-caution',
  LIMITED: 'text-sev-high',
  RISKY: 'text-sev-critical',
};

/**
 * 켜기 확인창의 등급 경고. 제한·위험은 「이 GPU 로는 ○○ 가 안 됩니다」를 보이고 확인을 거쳐 켠다(막지 않음).
 * 문구는 부팅의 서비스 축소(deploy/scripts/simai_gpu_boot.py boot_services)와 맞춘다: 24~47GB 는 Tier 0 관제만(분석 워커가 없어 세팅도 안 됨).
 */
export function tierWarning(tier: GpuTier | null | undefined): string | null {
  switch (tier) {
    case 'LIMITED':
      return '이 GPU 로는 세팅(맵 굽기)·VLM 근거·아바타가 안 됩니다. VRAM 48GB급 미만이라 Tier 0 관제만 켭니다.';
    case 'RISKY':
      return '이 GPU 로는 관제 준비가 실패할 수 있습니다. VRAM 24GB급 미만이라 Tier 0 최소 사양에 못 미치고 세팅·VLM 근거·아바타도 안 됩니다(개발·시험용).';
    case 'BELOW_RECOMMENDED':
      return 'VRAM 80GB급 미만이라 VLM 근거·아바타가 메모리 부족으로 늦거나 빠질 수 있습니다.';
    default:
      return null;
  }
}

/** 확인을 한 번 더 받아야 하는 등급(제한·위험) */
export const needsTierConfirm = (tier: GpuTier | null | undefined) =>
  tier === 'LIMITED' || tier === 'RISKY';

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
        return '시간당 가격이 GPU 설정(설정 → GPU)의 상한보다 높습니다.';
      case GPU_ERROR.STOP_BLOCKED_LIVE:
        return `${STOP_BLOCK_TEXT.LIVE}. 작업을 먼저 종료하세요.`;
      case GPU_ERROR.STOP_BLOCKED_SETUP:
        return `${STOP_BLOCK_TEXT.SETUP}. 세팅을 끝내거나 취소하세요.`;
      case GPU_ERROR.STOP_UNSUPPORTED:
        return '손으로 켠 GPU 는 여기서 끌 수 없습니다.';
      case GPU_ERROR.NOT_CONFIGURED:
        return 'GPU 공급자 설정(API 키 또는 템플릿 ID)이 없습니다. 설정의 GPU 구역을 확인하세요.';
      case GPU_ERROR.INVALID_SETTINGS:
        return '입력 값을 확인하세요.';
      case GPU_ERROR.PROVIDER_ERROR:
        return 'GPU 공급자 요청이 실패했습니다. 잠시 뒤 다시 시도하세요.';
    }
    if (error.status === 403) return '관리자만 할 수 있습니다.';
  }
  return '요청이 실패했습니다. 잠시 뒤 다시 시도하세요.';
}
