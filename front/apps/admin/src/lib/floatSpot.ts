import type { CSSProperties } from 'react';

// 떠 있는 상자(이벤트 패널·CCTV 창)의 자리를 재고 끌어 옮기는 계산. 자리 재기 외에는 화면 컴포넌트 없이 시험한다.

/** 상자 자리와 크기(부모 기준 px). */
export interface FloatBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** 부모(장면) 크기. */
export interface FloatBounds {
  width: number;
  height: number;
}

/** 부모 가장자리에 남기는 틈. 위는 상단 바 아래, 옆과 아래는 화면 가장자리다. */
export interface FloatMargin {
  top: number;
  side: number;
  bottom: number;
}

/** 떠 있는 상자가 화면 가장자리에 남기는 틈. 상단 바(60px) 아래에서 시작하고 이벤트 패널·CCTV 창이 같이 쓴다. */
export const FLOAT_MARGIN: FloatMargin = { top: 72, side: 14, bottom: 14 };

/** 어느 가장자리를 기준으로 자리를 잡는지. 위에 붙는 상자는 top, 아래에 붙는 상자는 bottom. */
export type FloatAnchor = 'top' | 'bottom';

/**
 * 끌어 옮긴 자리. offset 은 anchor 쪽 가장자리(위 또는 아래)에서 상자까지의 거리다.
 * right 가 있으면 오른쪽 가장자리에서 상자까지의 거리로 놓는다(left 는 놓을 때의 값이라 참고만 한다). 오른쪽 절반에 놓은
 * 상자는 창 폭이 바뀌어도 오른쪽 틈을 그대로 지켜 오른쪽 이벤트 패널과 끝이 맞는다.
 */
export interface FloatSpot {
  left: number;
  offset: number;
  right?: number;
}

/** 가장자리 틈에서 이만큼(px) 안쪽으로 놓으면 틈에 딱 붙인다(오른쪽 이벤트 패널·왼쪽 아래 평면 지도와 끝이 어긋나 보이지 않게). */
export const FLOAT_SNAP_PX = 16;

/** 값을 [min, max] 안에 둔다. max 가 min 보다 작으면(들어갈 자리가 없으면) min 에 둔다. */
export const clamp = (v: number, min: number, max: number) =>
  Math.min(Math.max(v, min), Math.max(min, max));

/** 시작 자리에서 끈 만큼 옮긴다. 틈을 남기고 부모 안에 머문다. */
export function moveSpot(
  start: FloatBox,
  dx: number,
  dy: number,
  bounds: FloatBounds,
  margin: FloatMargin,
  anchor: FloatAnchor,
): FloatSpot {
  const maxLeft = bounds.width - start.width - margin.side;
  const left = clamp(start.left + dx, margin.side, maxLeft);
  const maxTop = bounds.height - start.height - margin.bottom;
  const placedTop = clamp(start.top + dy, margin.top, maxTop);
  // 위·아래 틈 가까이 놓아도 틈에 붙인다(아래는 왼쪽 아래 평면 지도와 같은 높이가 된다).
  const top =
    maxTop >= margin.top && maxTop - placedTop <= FLOAT_SNAP_PX
      ? maxTop
      : placedTop - margin.top <= FLOAT_SNAP_PX
        ? margin.top
        : placedTop;
  const offset = anchor === 'top' ? top : bounds.height - top - start.height;
  // 들어갈 자리가 없으면(상자가 부모보다 크면) 왼쪽 틈에 붙인다.
  if (maxLeft < margin.side) return { left: margin.side, offset };
  if (left + start.width / 2 > bounds.width / 2) {
    const gap = bounds.width - left - start.width;
    const right = gap - margin.side <= FLOAT_SNAP_PX ? margin.side : gap;
    return { left: bounds.width - start.width - right, offset, right };
  }
  return { left: left - margin.side <= FLOAT_SNAP_PX ? margin.side : left, offset };
}

/** 옮긴 자리를 CSS 위치로. 기본 자리(클래스)보다 우선한다. right 가 있으면 left 대신 right 로 놓는다. */
export function spotStyle(spot: FloatSpot, anchor: FloatAnchor): CSSProperties {
  const side = spot.right === undefined ? { left: spot.left } : { right: spot.right };
  return anchor === 'top' ? { ...side, top: spot.offset } : { ...side, bottom: spot.offset };
}

/** 상자의 지금 자리와 부모(장면) 크기. 부모를 알 수 없으면 null. */
export type Measured = { box: FloatBox; bounds: FloatBounds } | null;

/** 화면에 그려진 상자의 자리와 부모 크기를 잰다(브라우저가 배치한 offset·client 크기를 읽는다). */
export function measure(el: HTMLElement | null): Measured {
  const parent = el?.offsetParent;
  if (!el || !(parent instanceof HTMLElement)) return null;
  return {
    box: { left: el.offsetLeft, top: el.offsetTop, width: el.offsetWidth, height: el.offsetHeight },
    bounds: { width: parent.clientWidth, height: parent.clientHeight },
  };
}
