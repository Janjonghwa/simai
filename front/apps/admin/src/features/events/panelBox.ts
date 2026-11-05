import { clamp, FLOAT_MARGIN, type FloatBounds, type FloatBox } from '@/lib/floatSpot';

// 떠 있는 이벤트 패널을 끌어 옮기고 크기를 바꾸는 계산(관제 데모와 같다). 화면 컴포넌트 없이 시험한다.
// 패널 자리와 부모 크기는 CCTV 창과 같은 타입(FloatBox·FloatBounds)이고, 가장자리 틈도 같이 쓴다.

export const PANEL_MIN_WIDTH = 280;
export const PANEL_MAX_WIDTH = 720;
export const PANEL_MIN_HEIGHT = 120;
/** 상단 바(60px) 아래와 화면 가장자리에 남기는 틈. */
export const PANEL_TOP = FLOAT_MARGIN.top;
export const PANEL_GAP = FLOAT_MARGIN.side;

/** 머리줄을 끈 만큼 옮긴다. 상단 바 아래·화면 안에 머물고, 아래로 내려 화면을 넘으면 높이를 줄인다. */
export function movePanel(start: FloatBox, dx: number, dy: number, bounds: FloatBounds): FloatBox {
  const top = clamp(start.top + dy, PANEL_TOP, bounds.height - PANEL_MIN_HEIGHT - PANEL_GAP);
  return {
    left: clamp(start.left + dx, PANEL_GAP, bounds.width - start.width - PANEL_GAP),
    top,
    width: start.width,
    height: clamp(start.height, PANEL_MIN_HEIGHT, bounds.height - top - PANEL_GAP),
  };
}

/** 손잡이: x 는 왼쪽 가장자리(폭), y 는 아래 가장자리(높이), xy 는 왼쪽 아래 모서리(둘 다). 오른쪽·위는 그대로다. */
export type PanelEdge = 'x' | 'y' | 'xy';

export function resizePanel(
  start: FloatBox,
  edge: PanelEdge,
  dx: number,
  dy: number,
  bounds: FloatBounds,
): FloatBox {
  const right = start.left + start.width;
  const width =
    edge === 'y'
      ? start.width
      : clamp(start.width - dx, PANEL_MIN_WIDTH, Math.min(PANEL_MAX_WIDTH, right - PANEL_GAP));
  const height =
    edge === 'x'
      ? start.height
      : clamp(start.height + dy, PANEL_MIN_HEIGHT, bounds.height - start.top - PANEL_GAP);
  return { left: right - width, top: start.top, width, height };
}

/** 창 크기가 바뀌어도 패널이 화면 밖으로 나가지 않게 다시 맞춘다. */
export function fitPanel(box: FloatBox, bounds: FloatBounds): FloatBox {
  const width = Math.min(box.width, Math.max(PANEL_MIN_WIDTH, bounds.width - 2 * PANEL_GAP));
  const top = clamp(box.top, PANEL_TOP, bounds.height - PANEL_MIN_HEIGHT - PANEL_GAP);
  return {
    width,
    top,
    left: clamp(box.left, PANEL_GAP, bounds.width - width - PANEL_GAP),
    height: clamp(box.height, PANEL_MIN_HEIGHT, bounds.height - top - PANEL_GAP),
  };
}
