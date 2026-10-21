import type { ZoneView } from '@/features/viewer3d/data/spatialFrame';
import { targetKey } from '@/features/viewer3d/target';
import type { ViewerTarget } from '@/features/viewer3d/types';
import { createContext } from 'react';

// 구역 표시 규칙. 색·투명도·선 모양·이름표를 한 곳에서 정한다. -185 위험 강조는 이 값을 바꾸지 않고 따로 얹는다.

/**
 * 구역을 어떤 적용 상태로 보일지.
 * - UNVERIFIED: 형상만 받았다. 기본값이다. ZoneSnapshot READY(형상 수신·검사 성공)나 viewer_health 의
 *   config.sync 만으로는 이 구역이 Tier 0 에 적용됐다고 보지 않는다.
 * - DEMO_PREVIEW / DEMO_ASSUMED_APPLIED: 개발 화면에서 표시 차이를 시험하는 값이다. 실제 저장·적용 성공이 아니다.
 * 구성 버전과 config_ack 를 BE·FE 가 검증해 넘기는 계약(BE -120·FE -189)이 정해지면, 그 증거로만 고르는 적용 상태를 여기에 더한다.
 */
export const ZONE_APPLY_STATES = ['UNVERIFIED', 'DEMO_PREVIEW', 'DEMO_ASSUMED_APPLIED'] as const;
export type ZoneApplyState = (typeof ZONE_APPLY_STATES)[number];

export const ZoneApplyContext = createContext<ZoneApplyState>('UNVERIFIED');

export interface ZoneStyle {
  color: number;
  fillOpacity: number;
  edgeOpacity: number;
  /** 점선이면 적용이 확인되지 않은 구역이다. */
  dashed: boolean;
  dashSize: number;
  gapSize: number;
  stateLabel: string;
}

// WebGL 재질은 CSS 토큰을 바로 읽지 못해 장면 전용 값을 여기에만 둔다.
// 구역 색은 경보색(빨강·주황)과 겹치지 않는 청록, 미리보기는 보라다. 구역 유형별 색은 유형 계약이 생기면 정한다.
const ZONE_COLOR = 0x4fb6c9;
const PREVIEW_COLOR = 0xa58bf0;
const SELECTED_COLOR = 0x6886d5;

const STYLES: Record<ZoneApplyState, ZoneStyle> = {
  UNVERIFIED: {
    color: ZONE_COLOR,
    fillOpacity: 0.08,
    edgeOpacity: 0.85,
    dashed: true,
    dashSize: 0.25,
    gapSize: 0.12,
    stateLabel: '적용 미확인',
  },
  DEMO_PREVIEW: {
    color: PREVIEW_COLOR,
    fillOpacity: 0.05,
    edgeOpacity: 0.9,
    dashed: true,
    dashSize: 0.08,
    gapSize: 0.08,
    stateLabel: '미리보기(DEMO)',
  },
  DEMO_ASSUMED_APPLIED: {
    color: ZONE_COLOR,
    fillOpacity: 0.16,
    edgeOpacity: 1,
    dashed: false,
    dashSize: 0,
    gapSize: 0,
    stateLabel: '적용 가정(DEMO·미확인)',
  },
};

/** 적용 상태와 선택 여부에 따른 모양. 선택은 색만 바꾸고 선 모양(적용 상태)은 그대로 둔다. */
export function zoneStyle(state: ZoneApplyState, selected: boolean): ZoneStyle {
  const style = STYLES[state];
  return selected
    ? { ...style, color: SELECTED_COLOR, fillOpacity: style.fillOpacity + 0.1, edgeOpacity: 1 }
    : style;
}

const meters = (value: number) => `${Math.round(value * 100) / 100}`;

/** 적용 상태의 화면 문구. */
export const zoneStateLabel = (state: ZoneApplyState) => STYLES[state].stateLabel;

/** 높이 범위(World Z) 문구. 예: `Z 1.2–2.4m`. */
export const zoneHeightText = (zone: Pick<ZoneView, 'min' | 'max'>) =>
  `Z ${meters(zone.min[2])}–${meters(zone.max[2])}m`;

/** 장면 이름표. ID·높이 범위(World Z)·적용 상태를 함께 적는다. */
export function zoneLabel(zone: Pick<ZoneView, 'zoneId' | 'min' | 'max'>, state: ZoneApplyState) {
  return [zone.zoneId, zoneHeightText(zone), STYLES[state].stateLabel].join(' · ');
}

const PERSON_PREFIX = targetKey({ type: 'person', id: '' });

/**
 * 클릭 광선에 걸린 가장 가까운 사람. 투명 구역은 이 사람을 대신 고른다(사람 > 구역).
 * 구역 안이나 뒤를 지나는 사람을 언제든 고를 수 있게 한다. 사람이 버튼을 누른 뒤 떼기 전에 광선 안으로
 * 걸어 들어와도 사람의 클릭 처리기는 R3F 규칙상 불리지 않으므로, 넘기지 않고 구역 처리기가 직접 고른다.
 */
export function personOnRay(
  intersections: readonly { eventObject: { name: string } }[],
): ViewerTarget | null {
  const hit = intersections.find((h) => h.eventObject.name.startsWith(PERSON_PREFIX));
  return hit ? { type: 'person', id: hit.eventObject.name.slice(PERSON_PREFIX.length) } : null;
}
