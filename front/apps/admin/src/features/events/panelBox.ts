// 떠 있는 이벤트 패널을 끌어 옮기고 크기를 바꾸는 계산(관제 데모와 같다). 화면 컴포넌트 없이 시험한다.

/** 패널 자리(부모 기준 px). */
export interface PanelBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface Bounds {
  width: number;
  height: number;
}

export const PANEL_MIN_WIDTH = 280;
export const PANEL_MAX_WIDTH = 720;
export const PANEL_MIN_HEIGHT = 120;
/** 상단 바(60px) 아래와 화면 가장자리에 남기는 틈. */
export const PANEL_TOP = 72;
export const PANEL_GAP = 14;

const clamp = (v: number, min: number, max: number) =>
  Math.min(Math.max(v, min), Math.max(min, max));

/** 머리줄을 끈 만큼 옮긴다. 상단 바 아래·화면 안에 머물고, 아래로 내려 화면을 넘으면 높이를 줄인다. */
export function movePanel(start: PanelBox, dx: number, dy: number, bounds: Bounds): PanelBox {
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
  start: PanelBox,
  edge: PanelEdge,
  dx: number,
  dy: number,
  bounds: Bounds,
): PanelBox {
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

/** 옮긴 패널이 차지한 가로 자리(px). right 는 부모 오른쪽 가장자리까지의 거리다. */
export interface PanelSpan {
  left: number;
  width: number;
  right: number;
}

/** 알림과 패널 사이 틈. 기본 자리(오른쪽 14px, 폭 340px)에서 알림 오른쪽 370px 과 같다. */
const NOTICE_GAP = 16;

/** 상단 알림(장애 띠·경보 배너)이 옮긴 패널을 비키는 자리. 패널 양옆 가운데 넓은 쪽에 둔다. */
export function noticeInset(span: PanelSpan): { left: number } | { right: number } {
  return span.left >= span.right
    ? { right: span.right + span.width + NOTICE_GAP }
    : { left: span.left + span.width + NOTICE_GAP };
}

/** 창 크기가 바뀌어도 패널이 화면 밖으로 나가지 않게 다시 맞춘다. */
export function fitPanel(box: PanelBox, bounds: Bounds): PanelBox {
  const width = Math.min(box.width, Math.max(PANEL_MIN_WIDTH, bounds.width - 2 * PANEL_GAP));
  const top = clamp(box.top, PANEL_TOP, bounds.height - PANEL_MIN_HEIGHT - PANEL_GAP);
  return {
    width,
    top,
    left: clamp(box.left, PANEL_GAP, bounds.width - width - PANEL_GAP),
    height: clamp(box.height, PANEL_MIN_HEIGHT, bounds.height - top - PANEL_GAP),
  };
}
