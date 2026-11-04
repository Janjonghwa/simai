import { PlanMinimap } from '@/features/plan/PlanMinimap';
import { DRAG_THRESHOLD_PX } from '@/hooks/useFloatSpot';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 3D 관제 왼쪽 아래의 작은 2D 지도: 몸통을 끌어 옮기고, 누르면 큰 창이 뜨고, 모서리를 끌어 크기를 바꾼다.
// 평면 그림(LivePlan)은 따로 시험하므로 자리표시자로 바꾼다.
vi.mock('@/features/plan/LivePlan', () => ({ LivePlan: () => <div data-testid="live-plan" /> }));

// jsdom 은 배치를 하지 않아 offset·client 크기가 모두 0 이다. 지도와 부모(장면)의 크기·자리를 속성으로 심는다.
const bounds = { width: 1000, height: 700 };
const place = { left: 14, top: 400, width: 200, height: 220 };

function stage() {
  const minimap = screen.getByTestId('plan-minimap');
  const parent = document.body;
  Object.defineProperties(parent, {
    clientWidth: { get: () => bounds.width, configurable: true },
    clientHeight: { get: () => bounds.height, configurable: true },
  });
  Object.defineProperties(minimap, {
    offsetParent: { get: () => parent, configurable: true },
    offsetLeft: { get: () => place.left, configurable: true },
    offsetTop: { get: () => place.top, configurable: true },
    offsetWidth: { get: () => place.width, configurable: true },
    offsetHeight: { get: () => place.height, configurable: true },
  });
  return minimap;
}

const at = (x: number, y: number) => ({ clientX: x, clientY: y, button: 0, buttons: 1 });
const opener = () => screen.getByRole('button', { name: '2D 평면 크게 보기' });

function renderMinimap() {
  render(<PlanMinimap selectedKey={null} onSelect={() => {}} />);
  return stage();
}

/** 몸통을 누르고 (x, y) 까지 끈 뒤 놓는다. */
function dragBody(minimap: HTMLElement, x: number, y: number) {
  fireEvent.pointerDown(minimap, at(100, 100));
  fireEvent.pointerMove(minimap, at(x, y));
  fireEvent.pointerUp(minimap, at(x, y));
}

beforeEach(() => {
  Object.assign(place, { left: 14, top: 400, width: 200, height: 220 });
});
afterEach(() => cleanup());

describe('PlanMinimap', () => {
  it('왼쪽 아래 가장자리에 붙고, 처음에는 위치 스타일 없이 폭 200px 이다', () => {
    const minimap = renderMinimap();
    expect(minimap.className).toContain('bottom-3.5');
    expect(minimap.className).toContain('left-3.5');
    expect(minimap.style.width).toBe('200px');
    expect(minimap.style.left).toBe('');
    expect(minimap.style.bottom).toBe('');
  });

  it('누르면 큰 2D 창이 열리고 Esc 로 닫힌다', () => {
    renderMinimap();
    expect(screen.queryByTestId('plan-modal')).toBeNull();
    fireEvent.click(opener());
    expect(screen.getByTestId('plan-modal')).toBeTruthy();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('plan-modal')).toBeNull();
  });

  it(`${DRAG_THRESHOLD_PX}px 넘게 끌면 옮겨지고, 놓은 뒤 이어지는 클릭은 창을 열지 않는다`, () => {
    const minimap = renderMinimap();
    dragBody(minimap, 400, 250);
    expect(minimap.style.left).not.toBe('');
    expect(minimap.style.bottom).not.toBe('');
    // 마우스로 놓은 뒤 따라 나오는 클릭(detail 1)이다.
    fireEvent.click(opener(), { detail: 1 });
    expect(screen.queryByTestId('plan-modal')).toBeNull();
    // 삼킨 것은 그 한 번뿐이라 다음 클릭은 열린다.
    fireEvent.click(opener(), { detail: 1 });
    expect(screen.getByTestId('plan-modal')).toBeTruthy();
  });

  it(`${DRAG_THRESHOLD_PX}px 이하로 움직인 뒤 누르면 옮겨지지 않고 클릭으로 열린다`, () => {
    const minimap = renderMinimap();
    fireEvent.pointerDown(minimap, at(100, 100));
    fireEvent.pointerMove(minimap, at(100 + DRAG_THRESHOLD_PX, 100));
    fireEvent.pointerUp(minimap, at(100 + DRAG_THRESHOLD_PX, 100));
    expect(minimap.style.left).toBe('');
    fireEvent.click(opener());
    expect(screen.getByTestId('plan-modal')).toBeTruthy();
  });

  it('끌어도 장면 가장자리 틈 밖으로 나가지 않는다', () => {
    const minimap = renderMinimap();
    dragBody(minimap, -5000, 5000);
    expect(minimap.style.left).toBe('14px');
    expect(minimap.style.bottom).toBe('14px');
  });

  it('모서리를 끌면 폭만 바뀌고 자리는 그대로이며, 폭은 160~480px 안이다', () => {
    const minimap = renderMinimap();
    const grip = minimap.querySelector<HTMLElement>('[aria-hidden].cursor-nesw-resize')!;
    fireEvent.pointerDown(grip, at(300, 100));
    fireEvent.pointerMove(grip, at(340, 100));
    expect(minimap.style.width).toBe('240px');
    fireEvent.pointerMove(grip, at(9000, 100));
    expect(minimap.style.width).toBe('480px');
    fireEvent.pointerMove(grip, at(-9000, 100));
    expect(minimap.style.width).toBe('160px');
    fireEvent.pointerUp(grip, at(-9000, 100));
    expect(minimap.style.left).toBe('');
  });

  it('오른쪽 끝으로 옮긴 지도의 폭을 키워 밖으로 넘치면 크기 조절이 끝날 때 안으로 다시 맞춘다', () => {
    const minimap = renderMinimap();
    dragBody(minimap, 5000, 250);
    expect(minimap.style.left).toBe('786px');
    place.left = 786;

    const grip = minimap.querySelector<HTMLElement>('[aria-hidden].cursor-nesw-resize')!;
    fireEvent.pointerDown(grip, at(300, 100));
    fireEvent.pointerMove(grip, at(400, 100));
    expect(minimap.style.width).toBe('300px');
    // 브라우저가 넓어진 폭을 배치에 반영했다고 본다.
    place.width = 300;
    fireEvent.pointerUp(grip, at(400, 100));
    expect(minimap.style.left).toBe(`${1000 - 300 - 14}px`);
  });

  it('모서리를 두 번 누르면 처음 크기와 처음 자리로 돌아간다', () => {
    const minimap = renderMinimap();
    dragBody(minimap, 400, 250);
    const grip = minimap.querySelector<HTMLElement>('[aria-hidden].cursor-nesw-resize')!;
    fireEvent.pointerDown(grip, at(300, 100));
    fireEvent.pointerMove(grip, at(360, 100));
    fireEvent.pointerUp(grip, at(360, 100));
    expect(minimap.style.width).toBe('260px');
    expect(minimap.style.left).not.toBe('');

    fireEvent.doubleClick(grip);
    expect(minimap.style.width).toBe('200px');
    expect(minimap.style.left).toBe('');
    expect(minimap.style.bottom).toBe('');
  });
});
