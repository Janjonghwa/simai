import type { PointerEvent } from 'react';

// 2D 평면 층이 함께 쓰는 그리기 값. 단위는 m(SVG 사용자 단위)이고 색은 토큰 클래스만 쓴다.

/** 등급 색 고리·채움(위험 빨강, 경고 주황, 주의 노랑). */
export const RING_TONE = {
  critical: 'fill-sev-critical/15 stroke-sev-critical',
  high: 'fill-sev-high/15 stroke-sev-high',
  caution: 'fill-sev-caution/15 stroke-sev-caution',
} as const;

/** 확인 대기 점멸. 움직임 줄이기 설정이면 멈추므로 선 굵기도 함께 키운다. */
export const ALARM_BLINK = 'motion-safe:animate-alarm-blink';

/**
 * 평면 표시 크기(-800). 큰 평면은 기본값이고, 작은 미니맵은 범위에 맞춰 사람 표식·구역 선을 키운다(planFit 의 minimapEmphasis).
 * 사람 위치 불확실 원은 실제 크기(m)라 배율을 받지 않는다.
 */
export interface PlanEmphasis {
  /** 사람 표식(점·방향·고리·누르는 자리)의 크기와 선 굵기 배율. */
  marker: number;
  /** 구역 선 굵기·점선 간격 배율. */
  zoneStroke: number;
  /** 구역 채움 불투명도(진행 중 이벤트가 없는 구역). */
  zoneFill: { readonly normal: number; readonly selected: number };
}

export const DEFAULT_EMPHASIS: PlanEmphasis = {
  marker: 1,
  zoneStroke: 1,
  zoneFill: { normal: 0.1, selected: 0.24 },
};

/** 대상마다 말풍선 머무름 핸들러를 붙인다(useDwell 의 on). */
export type Bind = (key: string) => {
  onPointerEnter: (e: PointerEvent) => void;
  onPointerMove: (e: PointerEvent) => void;
  onPointerLeave: () => void;
};
