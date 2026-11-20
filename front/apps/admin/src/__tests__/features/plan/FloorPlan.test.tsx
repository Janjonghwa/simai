import { FloorPlan } from '@/features/plan/FloorPlan';
import { planMarks, ROOM_BOUNDS, type PlanZone } from '@/features/plan/plan';
import { DEFAULT_EMPHASIS } from '@/features/plan/planDraw';
import { minimapEmphasis } from '@/features/plan/planFit';
import { velocityYaw } from '@/features/plan/planHeading';
import type { WorldTrack } from '@simai/contracts';
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(cleanup);

const track = (id: string, x: number, y: number, change: Partial<WorldTrack> = {}) =>
  ({
    world_id: id,
    position_xy: [x, y],
    position_xy_sigma: [0.1, 0.1],
    observation_state: 'OBSERVED',
    track_lifecycle: 'CONFIRMED',
    ...change,
  }) as unknown as WorldTrack;

const zone: PlanZone = { zoneId: 'z-1', name: '구역', min: [2, 3, 0], max: [4, 5, 1] };

/** 이 사람의 속도 방향을 진행 방향으로 준 기억(이어짐·뒤집힘 확인은 planHeading 시험이 본다). */
const headingsOf = (moving: WorldTrack) =>
  new Map([[moving.world_id, { yaw: velocityYaw(moving) }]]);

const NO_MARKS = planMarks([]);

function plan(props: Partial<React.ComponentProps<typeof FloorPlan>> = {}) {
  const view = render(<FloorPlan tracks={[]} zones={[]} marks={NO_MARKS} {...props} />);
  const svg = view.container.querySelector('svg')!;
  return { ...view, svg };
}

describe('2D 평면 방 범위', () => {
  it('범위를 주지 않으면 기본 10×10m 에 모눈을 그린다', () => {
    const { svg, container } = plan();
    // 여백 0.9m 를 양쪽에 더한 크기.
    expect(svg.getAttribute('viewBox')).toBe('-0.9 -0.9 11.8 11.8');
    expect(container.querySelector('[data-floor-plan-image]')).toBeNull();
    expect(container.querySelectorAll('g.stroke-border line')).toHaveLength(18);
  });

  it('주어진 범위로 viewBox 를 정한다', () => {
    const { svg } = plan({ bounds: { minX: -0.5, minY: -1, maxX: 12.5, maxY: 7 } });
    expect(svg.getAttribute('viewBox')).toBe('-0.9 -0.9 14.8 9.8');
  });

  it('범위의 왼쪽 위를 원점으로 사람과 구역을 놓는다', () => {
    const bounds = { minX: -0.5, minY: -1, maxX: 12.5, maxY: 7 };
    const { container } = plan({
      bounds,
      tracks: [track('w-1', 2, 3)],
      zones: [zone],
    });
    const rect = container.querySelector('rect[data-zone="z-1"]')!;
    // x = 2 - (-0.5), y = 7 - 5
    expect(Number(rect.getAttribute('x'))).toBeCloseTo(2.5);
    expect(Number(rect.getAttribute('y'))).toBeCloseTo(2);
    expect(Number(rect.getAttribute('width'))).toBeCloseTo(2);
    expect(Number(rect.getAttribute('height'))).toBeCloseTo(2);
    // 사람 점: x = 2.5, y = 4
    const dot = container.querySelector('circle.fill-foreground')!;
    expect(Number(dot.getAttribute('cx'))).toBeCloseTo(2.5);
    expect(Number(dot.getAttribute('cy'))).toBeCloseTo(4);
  });
});

describe('2D 평면 구역 모양', () => {
  const bounds = { minX: -0.5, minY: -1, maxX: 12.5, maxY: 7 };
  /** 회전한 직사각형(마름모) 구역. 둘레 상자는 [4,1]~[6,3]. */
  const rotated: PlanZone = {
    zoneId: 'z-r',
    name: '회전',
    min: [4, 1, 0],
    max: [6, 3, 2],
    polygon: [
      [5, 1],
      [6, 2],
      [5, 3],
      [4, 2],
    ],
  };

  it('회전했거나 직사각형이 아닌 구역은 실제 모양의 다각형으로 그린다(범위를 적용한 평면 좌표)', () => {
    const { container } = plan({ bounds, zones: [rotated] });
    const polygon = container.querySelector('polygon[data-zone="z-r"]')!;
    expect(container.querySelector('rect[data-zone="z-r"]')).toBeNull();
    // x = 값 + 0.5, y = 7 - 값
    expect(polygon.getAttribute('points')).toBe('5.5,6 6.5,5 5.5,4 4.5,5');
  });

  it('다각형 구역도 색·점선·선택이 같은 규칙을 따른다', () => {
    const critical = { ...rotated, severity: 'CRITICAL', judgeType: 'VOLUME_3D' } as const;
    const { container } = plan({ bounds, zones: [critical], selectedKey: 'zone:z-r' });
    const polygon = container.querySelector('polygon[data-zone="z-r"]')!;
    expect(polygon.getAttribute('class')).toContain('stroke-sev-critical');
    expect(polygon.getAttribute('fill-opacity')).toBe('0.24');
  });

  it('축에 맞춘 직사각형 다각형은 지금처럼 상자(rect)로 그린다', () => {
    const axis: PlanZone = {
      ...zone,
      polygon: [
        [2, 3],
        [4, 3],
        [4, 5],
        [2, 5],
      ],
    };
    const { container } = plan({ bounds, zones: [axis] });
    expect(container.querySelector('rect[data-zone="z-1"]')).not.toBeNull();
    expect(container.querySelector('polygon[data-zone="z-1"]')).toBeNull();
  });
});

describe('2D 평면 구역 색', () => {
  const zoneRect = (zoneProps: Partial<PlanZone>, selectedKey: string | null = null) => {
    const { container } = plan({ zones: [{ ...zone, ...zoneProps }], selectedKey });
    const element = container.querySelector('rect[data-zone="z-1"]')!;
    return {
      className: element.getAttribute('class') ?? '',
      dash: element.getAttribute('stroke-dasharray'),
      fillOpacity: Number(element.getAttribute('fill-opacity')),
    };
  };

  it.each([
    ['CRITICAL', 'stroke-sev-critical'],
    ['HIGH', 'stroke-sev-caution'],
    ['CAUTION', 'stroke-sev-caution'],
  ] as const)('구역 윤곽선은 심각도 %s 의 색(%s)이다', (severity, stroke) => {
    expect(zoneRect({ severity }).className).toContain(stroke);
  });

  it.each([undefined, 'INFO' as const])('심각도가 %s 인 구역은 중립색이다', (severity) => {
    const { className } = zoneRect({ severity });
    expect(className).toContain('stroke-muted-foreground');
    expect(className).not.toMatch(/stroke-sev-/);
  });

  it('입체 판정은 점선, 바닥 판정은 실선이다', () => {
    expect(zoneRect({ severity: 'HIGH', judgeType: 'VOLUME_3D' }).dash).not.toBeNull();
    expect(zoneRect({ severity: 'HIGH', judgeType: 'GROUND_FOOTPRINT' }).dash).toBeNull();
  });

  it('고른 구역도 심각도 색 그대로이고 채움만 짙어진다', () => {
    const idle = zoneRect({ severity: 'HIGH' });
    const selected = zoneRect({ severity: 'HIGH' }, 'zone:z-1');
    expect(selected.className).toContain('stroke-sev-caution');
    expect(selected.className).toBe(idle.className);
    expect(selected.fillOpacity).toBeGreaterThan(idle.fillOpacity);
  });
});

describe('평면도 바탕', () => {
  const bounds = { minX: -0.5, minY: -0.5, maxX: 10.5, maxY: 10.5 };

  it('이미지를 방 범위 전체에 깔고 모눈은 그리지 않는다', () => {
    const { container } = plan({ bounds, backdrop: { imageUrl: 'blob:floor-plan' } });
    const image = container.querySelector('[data-floor-plan-image]')!;
    expect(image.getAttribute('href')).toBe('blob:floor-plan');
    expect(image.getAttribute('width')).toBe('11');
    expect(image.getAttribute('height')).toBe('11');
    expect(container.querySelectorAll('g.stroke-border line')).toHaveLength(0);
  });

  it('이미지를 아직 받지 못했으면 모눈을 그린다', () => {
    const { container } = plan({ bounds, backdrop: { imageUrl: undefined } });
    expect(container.querySelector('[data-floor-plan-image]')).toBeNull();
    expect(container.querySelectorAll('g.stroke-border line').length).toBeGreaterThan(0);
  });

  it('방 외곽을 평면 좌표로 그린다', () => {
    const { container } = plan({
      bounds,
      backdrop: {
        outline: [
          [0, 0],
          [10, 0],
          [10, 10],
          [0, 10],
        ],
      },
    });
    const polygon = container.querySelector('[data-room-outline]')!;
    expect(polygon.getAttribute('points')).toBe('0.5,10.5 10.5,10.5 10.5,0.5 0.5,0.5');
  });

  it('오래된 평면도일 때만 그 표시를 한다', () => {
    const fresh = plan({ bounds, backdrop: { imageUrl: 'blob:x', outdated: false } });
    expect(fresh.container.querySelector('[data-floor-plan-outdated]')).toBeNull();
    fresh.unmount();
    const old = plan({ bounds, backdrop: { imageUrl: 'blob:x', outdated: true } });
    expect(old.container.querySelector('[data-floor-plan-outdated]')?.textContent).toBe(
      '오래된 평면도',
    );
  });
});

describe('2D 평면 바닥 감시 범위(-606)', () => {
  const coverage = {
    calibrationVersion: 'cal-1',
    cellM: 0.5,
    originXy: [1, 2] as const,
    cols: 2,
    rows: 2,
    cells: '0.22',
  };

  it('칸 묶음을 구역 아래에 깔고 World +Y 를 화면 위로 뒤집는다', () => {
    const { container } = plan({ coverage, zones: [zone] });
    const rects = [...container.querySelectorAll('[data-plan-coverage] rect')];
    expect(rects.map((r) => r.getAttribute('data-coverage'))).toEqual(['blind', 'covered']);
    // 사각 칸 World [1,1.5]×[2,2.5] → 평면 (x - 0, 10 - 2.5)
    expect(rects[0]?.getAttribute('x')).toBe('1');
    expect(rects[0]?.getAttribute('y')).toBe('7.5');
    expect(rects[1]?.getAttribute('width')).toBe('1');
    expect(rects[1]?.getAttribute('y')).toBe('7');
    // 구역보다 먼저(아래에) 그린다
    const svgChildren = [...container.querySelector('svg')!.children];
    const layerIndex = svgChildren.findIndex((el) => el.hasAttribute('data-plan-coverage'));
    expect(layerIndex).toBeGreaterThan(-1);
    expect(layerIndex).toBeLessThan(svgChildren.length - 2);
  });

  it('넘기지 않으면 그리지 않는다', () => {
    const { container } = plan();
    expect(container.querySelector('[data-plan-coverage]')).toBeNull();
  });
});

describe('사람 진행 방향 표식(-800)', () => {
  const bounds = { minX: -0.5, minY: -1, maxX: 12.5, maxY: 7 };

  /** 사람 점의 중심(평면 좌표). */
  const dotOf = (container: HTMLElement) => {
    const dot = container.querySelector('circle.fill-foreground')!;
    return [Number(dot.getAttribute('cx')), Number(dot.getAttribute('cy'))] as const;
  };
  /** 방향 표식의 끝 꼭짓점(평면 좌표). 없으면 null. */
  const tipOf = (container: HTMLElement) => {
    const points = container.querySelector('polygon[data-heading]')?.getAttribute('points');
    return points ? (points.split(' ')[0]!.split(',').map(Number) as [number, number]) : null;
  };
  const unit = ([x, y]: readonly [number, number]) => {
    const length = Math.hypot(x, y);
    return [x / length, y / length];
  };

  it.each([
    ['+X', [1, 0, 0], [1, 0]],
    ['+Y', [0, 1, 0], [0, 1]],
    ['−X−Y', [-0.5, -0.5, 0], [-1, -1]],
  ] as const)(
    '%s 로 움직이는 사람의 방향 표식은 그 사람이 그쪽으로 옮겨 간 화면 방향을 가리킨다',
    (_, velocity, step) => {
      const moving = track('w-1', 2, 3, { velocity: [...velocity] });
      const headings = headingsOf(moving);
      const before = plan({ bounds, tracks: [moving], headings });
      const center = dotOf(before.container);
      const tip = tipOf(before.container)!;
      before.unmount();
      // 같은 사람이 그 방향으로 1m 옮겨 간 자리.
      const after = plan({ bounds, tracks: [track('w-1', 2 + step[0], 3 + step[1])] });
      const moved = dotOf(after.container);
      const pointing = unit([tip[0] - center[0], tip[1] - center[1]]);
      const travel = unit([moved[0] - center[0], moved[1] - center[1]]);
      expect(pointing[0]).toBeCloseTo(travel[0]);
      expect(pointing[1]).toBeCloseTo(travel[1]);
    },
  );

  it('World +Y 로 걸으면 화면 위쪽(−y)을 가리킨다', () => {
    const moving = track('w-1', 2, 3, { velocity: [0, 1, 0] });
    const { container } = plan({
      bounds,
      tracks: [moving],
      headings: headingsOf(moving),
    });
    const [cx, cy] = dotOf(container);
    const [tx, ty] = tipOf(container)!;
    expect(tx).toBeCloseTo(cx);
    expect(ty).toBeLessThan(cy);
  });

  it('방향을 모르면 방향 표식 없이 점만 그리고, 확인 불가 사람에게는 방향 표식을 붙이지 않는다', () => {
    const still = plan({ bounds, tracks: [track('w-1', 2, 3)], headings: new Map() });
    expect(tipOf(still.container)).toBeNull();
    expect(still.container.querySelector('circle.fill-foreground')).not.toBeNull();
    still.unmount();
    const unknown = track('w-1', 2, 3, { observation_state: 'UNKNOWN' });
    const { container } = plan({
      bounds,
      tracks: [unknown],
      headings: new Map([['w-1', { yaw: 0 }]]),
    });
    expect(tipOf(container)).toBeNull();
  });
});

describe('미니맵 범위·표시 크기(-800)', () => {
  const fitted = { minX: 2, minY: 3, maxX: 8, maxY: 9 };

  it('비출 범위를 주면 그 범위로 viewBox 를 정한다(왼쪽 위는 min x, max y)', () => {
    const { svg } = plan({ bounds: ROOM_BOUNDS, view: fitted });
    // toPlan(2, 9) = (2, 10 - 9)
    expect(svg.getAttribute('viewBox')).toBe('2 1 6 6');
  });

  it('표시 배율만큼 사람 점·누르는 자리를 키우고, 구역 선을 굵게·채움을 짙게 한다', () => {
    const emphasis = minimapEmphasis({ minX: 0, minY: 0, maxX: 24, maxY: 24 });
    const props = { tracks: [track('w-1', 3, 4)], zones: [zone] };
    const normal = plan(props);
    const radius = (c: HTMLElement) =>
      Number(c.querySelector('circle.fill-foreground')!.getAttribute('r'));
    const zoneAttr = (c: HTMLElement, name: string) =>
      Number(c.querySelector('rect[data-zone="z-1"]')!.getAttribute(name));
    const base = {
      r: radius(normal.container),
      stroke: zoneAttr(normal.container, 'stroke-width'),
      fill: zoneAttr(normal.container, 'fill-opacity'),
    };
    normal.unmount();
    const big = plan({ ...props, emphasis });
    expect(radius(big.container)).toBeCloseTo(base.r * emphasis.marker);
    expect(zoneAttr(big.container, 'stroke-width')).toBeCloseTo(base.stroke * emphasis.zoneStroke);
    expect(zoneAttr(big.container, 'fill-opacity')).toBeGreaterThan(base.fill);
    expect(base.fill).toBe(DEFAULT_EMPHASIS.zoneFill.normal);
  });

  it('맞춘 범위·배율·방향 표식이 있어도 사람·구역을 누르면 고른다', () => {
    const onSelect = vi.fn();
    const moving = track('w-1', 5, 6, { velocity: [1, 0, 0] });
    const { container } = plan({
      bounds: ROOM_BOUNDS,
      view: fitted,
      emphasis: minimapEmphasis(fitted),
      headings: headingsOf(moving),
      tracks: [moving],
      zones: [zone],
      onSelect,
      selectedKey: 'person:w-1',
    });
    const person = container.querySelector('[data-person="w-1"]')!;
    expect(person.getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(person);
    expect(onSelect).toHaveBeenLastCalledWith('person:w-1');
    fireEvent.click(container.querySelector('rect[data-zone="z-1"]')!);
    expect(onSelect).toHaveBeenLastCalledWith('zone:z-1');
    // 고른 사람의 선택 고리도 그대로 그린다.
    expect(container.querySelector('circle.stroke-primary')).not.toBeNull();
  });
});
