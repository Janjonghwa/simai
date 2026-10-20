import type { ViewerTarget } from '@/features/viewer3d/types';

// 목업 전용 값. 실제 관측·판정·좌표가 아니다(World 좌표 정합은 S15P31A101-38).

/** ?mockUi=0|false|off 이면 목업 UI 를 끈다. 그 밖에는 켠다. */
export function readMockUi(search: string): boolean {
  const value = new URLSearchParams(search).get('mockUi');
  return value === null || !['0', 'false', 'off'].includes(value.toLowerCase());
}

/** 다른 매개변수·해시를 그대로 두고 mockUi 만 바꾼 주소. */
export function withMockUi(href: string, on: boolean): string {
  const url = new URL(href);
  url.searchParams.set('mockUi', on ? '1' : '0');
  return url.toString();
}

export const MENUS = [
  { key: 'monitor', label: '실시간 관제' },
  { key: 'cctv', label: 'CCTV 관제' },
  { key: 'zones', label: '구역 편집' },
  { key: 'tbm', label: 'TBM 보고서' },
  { key: 'system', label: '시스템 상태' },
] as const;

export type MenuKey = (typeof MENUS)[number]['key'];

export interface FixturePerson {
  id: string;
  name: string;
  x: number;
  z: number;
}

export interface FixtureBox {
  id: string;
  name: string;
  x: number;
  z: number;
  width: number;
  depth: number;
  height: number;
}

export interface FixtureZone extends FixtureBox {
  tone: 'critical' | 'caution';
}

// Three.js 장면 단위(Y 상방)로 바로 둔 예시 배치. 10×10 교실을 추상화했다.
export const PEOPLE: FixturePerson[] = [
  { id: 'worker1', name: '예시 작업자 1', x: -2.6, z: -2.2 },
  { id: 'worker2', name: '예시 작업자 2', x: 1.2, z: 0.6 },
  { id: 'worker3', name: '예시 작업자 3', x: 2.8, z: 2.4 },
];

export const ZONES: FixtureZone[] = [
  {
    id: 'zoneA',
    name: '예시 구역 A',
    x: -3,
    z: -3,
    width: 3,
    depth: 2.4,
    height: 2,
    tone: 'critical',
  },
  {
    id: 'zoneB',
    name: '예시 구역 B',
    x: 3,
    z: 3,
    width: 2.6,
    depth: 2,
    height: 1.4,
    tone: 'caution',
  },
];

export const OBJECTS: FixtureBox[] = [
  { id: 'surface1', name: '예시 작업면', x: 0.4, z: -3.6, width: 2.2, depth: 0.9, height: 0.8 },
];

export const CAMERAS = [
  { id: 'cam1', x: -5, z: -5 },
  { id: 'cam2', x: 5, z: -5 },
  { id: 'cam3', x: -5, z: 5 },
  { id: 'cam4', x: 5, z: 5 },
];

export interface FixtureEvent {
  id: string;
  severity: 'HIGH' | 'CAUTION';
  title: string;
  target: ViewerTarget;
}

export const EVENTS: FixtureEvent[] = [
  {
    id: 'event1',
    severity: 'HIGH',
    title: '구역 진입 예시',
    target: { type: 'person', id: 'worker1' },
  },
  {
    id: 'event2',
    severity: 'CAUTION',
    title: '진입 예측 예시',
    target: { type: 'person', id: 'worker3' },
  },
];

/** 선택 대상의 표시 이름. 목업 목록에 없으면 null. */
export function fixtureName(target: ViewerTarget): string | null {
  const list = target.type === 'person' ? PEOPLE : target.type === 'zone' ? ZONES : OBJECTS;
  return list.find((item) => item.id === target.id)?.name ?? null;
}
