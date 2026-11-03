import { FloorPlan } from '@/features/plan/FloorPlan';
import { planMarks, type PlanZone } from '@/features/plan/plan';
import type { WorldTrack } from '@simai/contracts';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

afterEach(cleanup);

const track = (id: string, x: number, y: number) =>
  ({
    world_id: id,
    position_xy: [x, y],
    position_xy_sigma: [0.1, 0.1],
    observation_state: 'OBSERVED',
    track_lifecycle: 'CONFIRMED',
  }) as unknown as WorldTrack;

const zone: PlanZone = { zoneId: 'z-1', name: '구역', min: [2, 3, 0], max: [4, 5, 1] };

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
    ['HIGH', 'stroke-sev-high'],
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
    expect(selected.className).toContain('stroke-sev-high');
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
