import { measure, moveSpot, spotStyle, type FloatBox } from '@/lib/floatSpot';
import { describe, expect, it } from 'vitest';

const bounds = { width: 1000, height: 700 };
const margin = { top: 72, side: 14, bottom: 14 };
const start: FloatBox = { left: 100, top: 200, width: 120, height: 40 };

describe('moveSpot', () => {
  it('끈 만큼 옮긴다. 위 기준은 위에서, 아래 기준은 아래에서 잰다', () => {
    expect(moveSpot(start, 50, 30, bounds, margin, 'top')).toEqual({ left: 150, offset: 230 });
    // 아래 offset = 700 − 위 230 − 높이 40
    expect(moveSpot(start, 50, 30, bounds, margin, 'bottom')).toEqual({ left: 150, offset: 430 });
  });

  it('움직이지 않으면 자리가 그대로다(창 크기를 다시 맞출 때 쓴다)', () => {
    expect(moveSpot(start, 0, 0, bounds, margin, 'top')).toEqual({ left: 100, offset: 200 });
  });

  it('틈을 남기고 부모 안에 머문다', () => {
    expect(moveSpot(start, -5000, -5000, bounds, margin, 'top')).toEqual({ left: 14, offset: 72 });
    // 오른쪽 끝까지 끌면 오른쪽 틈(right)으로 놓는다.
    expect(moveSpot(start, 5000, 5000, bounds, margin, 'top')).toEqual({
      left: 1000 - 120 - 14,
      offset: 700 - 40 - 14,
      right: 14,
    });
    expect(moveSpot(start, -5000, -5000, bounds, margin, 'bottom')).toEqual({
      left: 14,
      offset: 700 - 72 - 40,
    });
    expect(moveSpot(start, 5000, 5000, bounds, margin, 'bottom')).toEqual({
      left: 1000 - 120 - 14,
      offset: 14,
      right: 14,
    });
  });

  it('상자가 부모보다 커서 들어갈 자리가 없으면 위·왼쪽 틈에 붙는다', () => {
    const big: FloatBox = { left: 0, top: 0, width: 2000, height: 900 };
    expect(moveSpot(big, 300, 300, bounds, margin, 'top')).toEqual({ left: 14, offset: 72 });
  });
});

describe('moveSpot 오른쪽 기준·틈 붙이기', () => {
  it('오른쪽 절반에 놓으면 오른쪽 틈(right)으로 놓아 창 폭이 바뀌어도 오른쪽 끝이 그대로다', () => {
    // 가운데 = 600 + 60 = 660 > 500 → 오른쪽 틈 = 1000 − 600 − 120 = 280
    expect(moveSpot(start, 500, 0, bounds, margin, 'bottom')).toEqual({
      left: 600,
      offset: 700 - 200 - 40,
      right: 280,
    });
  });

  it('옆 틈에서 16 px 안쪽으로 놓으면 틈에 딱 붙인다(오른쪽 이벤트 패널과 끝이 맞는다)', () => {
    // 오른쪽 틈 = 1000 − 850 − 120 = 30 → 30 − 14 = 16 이하라 14 로 붙인다.
    expect(moveSpot(start, 750, 0, bounds, margin, 'top')).toEqual({
      left: 1000 - 120 - 14,
      offset: 200,
      right: 14,
    });
    // 오른쪽 틈 31 은 그대로 둔다.
    expect(moveSpot(start, 749, 0, bounds, margin, 'top')).toMatchObject({ right: 31 });
    // 왼쪽도 같다: 왼쪽 30 → 14.
    expect(moveSpot(start, -70, 0, bounds, margin, 'top')).toEqual({ left: 14, offset: 200 });
    expect(moveSpot(start, -69, 0, bounds, margin, 'top')).toEqual({ left: 31, offset: 200 });
  });

  it('위·아래 틈에서 16 px 안쪽으로 놓아도 틈에 붙인다', () => {
    // 아래 틈 = 700 − 630 − 40 = 30 → 아래 offset 14
    expect(moveSpot(start, 0, 430, bounds, margin, 'bottom')).toEqual({ left: 100, offset: 14 });
    // 위 = 88 → 72
    expect(moveSpot(start, 0, -112, bounds, margin, 'top')).toEqual({ left: 100, offset: 72 });
    expect(moveSpot(start, 0, -111, bounds, margin, 'top')).toEqual({ left: 100, offset: 89 });
  });
});

describe('spotStyle', () => {
  it('오른쪽 틈(right)이 있으면 left 대신 right 로 놓는다', () => {
    expect(spotStyle({ left: 866, offset: 14, right: 14 }, 'bottom')).toEqual({
      right: 14,
      bottom: 14,
    });
  });

  it('위 기준은 left·top, 아래 기준은 left·bottom 으로 바꾼다', () => {
    expect(spotStyle({ left: 150, offset: 230 }, 'top')).toEqual({ left: 150, top: 230 });
    expect(spotStyle({ left: 150, offset: 430 }, 'bottom')).toEqual({ left: 150, bottom: 430 });
  });
});

describe('measure', () => {
  it('그려진 상자의 자리·크기와 부모(offsetParent) 크기를 읽는다', () => {
    const parent = document.createElement('div');
    const box = document.createElement('div');
    parent.append(box);
    // jsdom 은 배치를 하지 않아 크기·자리를 속성으로 심는다.
    Object.defineProperties(parent, {
      clientWidth: { get: () => 800 },
      clientHeight: { get: () => 600 },
    });
    Object.defineProperties(box, {
      offsetParent: { get: () => parent },
      offsetLeft: { get: () => 10 },
      offsetTop: { get: () => 20 },
      offsetWidth: { get: () => 30 },
      offsetHeight: { get: () => 40 },
    });

    expect(measure(box)).toEqual({
      box: { left: 10, top: 20, width: 30, height: 40 },
      bounds: { width: 800, height: 600 },
    });
  });

  it('상자가 없거나 부모를 알 수 없으면 null 이다', () => {
    expect(measure(null)).toBeNull();
    // jsdom 은 offsetParent 가 항상 null 이다(배치가 없다).
    expect(measure(document.createElement('div'))).toBeNull();
  });
});
