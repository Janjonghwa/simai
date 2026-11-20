import {
  clamp,
  FLOAT_MARGIN,
  measure,
  type FloatBounds,
  type FloatBox,
  type FloatMargin,
} from '@/lib/floatSpot';
import {
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from 'react';

// 실시간 관제 아래쪽 두 창(왼쪽 미니맵·오른쪽 CCTV)의 비율 고정 모서리 크기 조절 공용 훅과 경계 계산.
// 두 창이 서로, 그리고 이벤트 패널·작업 표시줄(상단 알림)·도구 모음과 겹치지 않게 최대 크기를 제한한다.

/** index.css 의 --side-column-width(260px) 와 같은 값. 미니맵 기본 크기의 상한이다. */
export const SIDE_COLUMN_WIDTH = 260;
/** index.css 의 34svh 와 같은 비율. 낮은 화면에서 미니맵 기본 크기를 정한다. */
export const MINIMAP_MAX_VH_RATIO = 0.34;
/** 이벤트 패널이 CCTV 를 피해 줄어들 수 있는 최소 높이(features/events/panelBox 의 PANEL_MIN_HEIGHT 와 같다). */
const EVENT_PANEL_MIN_HEIGHT = 120;
/** 두 창과 주변 패널 사이에 남기는 기본 간격(px). */
const PANEL_GAP = FLOAT_MARGIN.side;

/** 어느 안쪽 모서리에 손잡이를 두는지. 미니맵은 오른쪽 위, CCTV 창은 왼쪽 위다. */
export type ResizeCorner = 'top-right' | 'top-left';

/** 현재 화면 높이에서 미니맵 기본 크기(px). CSS `--minimap-size: min(260px, 34svh)` 와 일치한다. */
export function defaultMinimapSize(viewportHeight: number): number {
  return Math.round(Math.min(SIDE_COLUMN_WIDTH, viewportHeight * MINIMAP_MAX_VH_RATIO));
}

/** 아래 창이 커질 수 있는 최대 높이(px): 화면 짧은 변의 절반. */
export function maxBottomPanelHeight(bounds: FloatBounds): number {
  return Math.floor(Math.min(bounds.width, bounds.height) / 2);
}

/**
 * 모서리 끌기 변위(dx, dy)를 비율 고정 새 폭(px)으로 바꾼다.
 * 가로·세로 축 중 변화량이 큰 쪽을 따라가므로 대각선·가로·세로 어느 쪽으로 끌어도 커서를 자연스럽게 따른다.
 */
export function widthFromDrag(
  startWidth: number,
  dx: number,
  dy: number,
  corner: ResizeCorner,
  aspect: number,
): number {
  const signX = corner === 'top-right' ? 1 : -1;
  const deltaX = signX * dx;
  // 두 손잡이 모두 위쪽 모서리라 위로 끌수록(dy < 0) 커진다.
  const deltaY = -dy * aspect;
  const delta = Math.abs(deltaX) >= Math.abs(deltaY) ? deltaX : deltaY;
  return Math.round(startWidth + delta);
}

export interface MaxWidthParams {
  corner: ResizeCorner;
  /** 고정 모서리 좌표(부모 기준 px). top-right 면 왼쪽 아래, top-left 면 오른쪽 아래다. */
  anchor: { x: number; bottom: number };
  /** 가로 / 세로 비율. 정사각형이면 1, 16:9 면 16 / 9. */
  aspect: number;
  bounds: FloatBounds;
  obstacles: readonly FloatBox[];
  margin?: FloatMargin;
  gap?: number;
}

/**
 * 고정 모서리에서 화면 안쪽으로 비율을 유지하며 키울 때, 화면 짧은 변의 절반·화면 가장자리·주변 장애물과
 * 겹치지 않는 최대 폭(px)을 계산한다.
 */
export function maxWidthAgainstObstacles({
  corner,
  anchor,
  aspect,
  bounds,
  obstacles,
  margin = FLOAT_MARGIN,
  gap = PANEL_GAP,
}: MaxWidthParams): number {
  let maxH = maxBottomPanelHeight(bounds);
  maxH = Math.min(maxH, anchor.bottom - margin.top);

  const sideRoom =
    corner === 'top-right' ? bounds.width - margin.side - anchor.x : anchor.x - margin.side;
  maxH = Math.min(maxH, sideRoom / aspect);

  for (const obs of obstacles) {
    if (obs.width <= 0 || obs.height <= 0) continue;
    // 고정 바닥이 이미 장애물 위쪽이면 위로 커져도 닿지 않는다.
    if (anchor.bottom + gap <= obs.top) continue;

    if (corner === 'top-right') {
      if (anchor.x >= obs.left + obs.width + gap) continue;
      const hSide = (obs.left - gap - anchor.x) / aspect;
      const hBelow = anchor.bottom - (obs.top + obs.height + gap);
      maxH = Math.min(maxH, Math.max(hSide, hBelow));
    } else {
      if (anchor.x <= obs.left - gap) continue;
      const hSide = (anchor.x - (obs.left + obs.width + gap)) / aspect;
      const hBelow = anchor.bottom - (obs.top + obs.height + gap);
      maxH = Math.min(maxH, Math.max(hSide, hBelow));
    }
  }

  return Math.max(0, Math.floor(maxH * aspect));
}

/** 요소를 container 기준 좌표(FloatBox)로 잰다. 보이지 않거나 크기가 0 이면 null. */
function measureInContainer(el: Element, container: HTMLElement): FloatBox | null {
  const hostRect = container.getBoundingClientRect();
  const rect = el.getBoundingClientRect();
  if (rect.width > 0 && rect.height > 0 && hostRect.width > 0 && hostRect.height > 0) {
    return {
      left: rect.left - hostRect.left,
      top: rect.top - hostRect.top,
      width: rect.width,
      height: rect.height,
    };
  }
  if (
    el instanceof HTMLElement &&
    el.offsetParent === container &&
    el.offsetWidth > 0 &&
    el.offsetHeight > 0
  ) {
    return {
      left: el.offsetLeft,
      top: el.offsetTop,
      width: el.offsetWidth,
      height: el.offsetHeight,
    };
  }
  return null;
}

/**
 * 실시간 관제 화면에서 아래 창(미니맵·CCTV)이 겹치면 안 되는 주변 상자 목록을 수집한다.
 * - 상대 아래 창(미니맵 ↔ CCTV)
 * - 이벤트 패널(CCTV 는 이벤트 패널이 최소 높이까지 자동으로 비키므로 최소 높이 구간만 막는다)
 * - 도구 모음
 * - 상단 알림 영역 안의 작업 표시줄(작업 세션·관제 상태·경보 배너 등)
 */
export function collectBottomPanelObstacles(
  container: HTMLElement,
  self: HTMLElement | null,
  options?: { shrinkableEventPanel?: boolean },
): FloatBox[] {
  const seen = new Set<Element>();
  const obstacles: FloatBox[] = [];
  const push = (el: Element | null, transform?: (box: FloatBox) => FloatBox) => {
    if (!el || el === self || seen.has(el)) return;
    seen.add(el);
    const box = measureInContainer(el, container);
    if (box) obstacles.push(transform ? transform(box) : box);
  };

  push(container.querySelector('[data-testid="plan-minimap"]'));
  push(container.querySelector('[aria-label="떠 있는 CCTV"]'));
  push(container.querySelector('[role="group"][aria-label="도구"]'));

  const eventPanel = container.querySelector('aside[aria-label="이벤트"]');
  push(eventPanel, (box) =>
    options?.shrinkableEventPanel
      ? { ...box, height: Math.min(box.height, EVENT_PANEL_MIN_HEIGHT) }
      : box,
  );

  // 상단 알림 영역(AppLayout 의 Notices)은 관제 main 밖의 형제이므로 루트에서 찾는다.
  const searchRoot =
    container.closest('.h-svh') ?? container.parentElement?.parentElement ?? document;
  const notices = searchRoot.querySelector('[role="region"][aria-label="알림"]');
  if (notices) {
    for (const child of Array.from(notices.children)) {
      push(child);
    }
  }
  // 단독 시험처럼 알림 래퍼 없이 작업 표시줄만 둔 경우도 바로 잡는다.
  for (const bar of searchRoot.querySelectorAll(
    '[aria-label="작업 세션"], [aria-label="작업 종료"], [aria-label="관제 상태"]',
  )) {
    push(bar);
  }

  return obstacles;
}

function anchorOf(box: FloatBox, corner: ResizeCorner) {
  return {
    x: corner === 'top-right' ? box.left : box.left + box.width,
    bottom: box.top + box.height,
  };
}

interface ActiveDrag {
  startX: number;
  startY: number;
  startBox: FloatBox;
  minW: number;
  maxW: number;
  lastW: number;
  moved: boolean;
}

export interface UseAspectResizeOptions {
  target: RefObject<HTMLElement | null>;
  corner: ResizeCorner;
  /** 가로 / 세로 비율. */
  aspect: number;
  /** 사용자가 조절한 폭(px). null 이면 기본 크기를 쓴다. */
  width: number | null;
  /** 현재 부모 크기 기준 최소 폭(px, 기본 크기와 같다). */
  minWidthOf: (bounds: FloatBounds) => number;
  /** 겹치면 안 되는 장애물 목록을 수집한다. */
  getObstacles?: (container: HTMLElement) => FloatBox[];
  /** 끄는 동안 새 폭(px)과 시작 상자를 알린다. */
  onResize: (nextWidth: number, startBox: FloatBox) => void;
  /** 끌기를 마쳤을 때 최종 폭과 최소 폭을 알린다(저장·기본 복귀 판정용). */
  onResizeEnd?: (finalWidth: number, minWidth: number) => void;
  /** 손잡이를 두 번 눌러 기본 크기로 되돌린다. */
  onReset?: () => void;
}

/**
 * 비율을 고정한 채 안쪽 모서리 손잡이로 창 크기를 바꾼다.
 * - 창 크기 변경·재진입 시에도 현재 화면·주변 창 한계 안으로 유효 폭을 맞춘다.
 * - 손잡이의 포인터·클릭 이벤트는 전파를 막아 몸통 끌기나 클릭 열기와 겹치지 않게 한다.
 */
export function useAspectResize({
  target,
  corner,
  aspect,
  width,
  minWidthOf,
  getObstacles,
  onResize,
  onResizeEnd,
  onReset,
}: UseAspectResizeOptions) {
  const [resizing, setResizing] = useState(false);
  const [limit, setLimit] = useState<{ min: number; max: number } | null>(null);
  const drag = useRef<ActiveDrag | null>(null);

  // 저장된 크기가 있을 때 첫 페인트 전과 화면 크기 변경 시 현재 화면·주변 창 한계 안으로 맞춘다.
  useLayoutEffect(() => {
    if (width === null || resizing) return undefined;
    const fit = () => {
      const el = target.current;
      const now = measure(el);
      const container = el?.offsetParent;
      if (!now || !(container instanceof HTMLElement)) {
        setLimit(null);
        return;
      }
      const min = minWidthOf(now.bounds);
      const obstacles = getObstacles?.(container) ?? [];
      const max = Math.max(
        min,
        maxWidthAgainstObstacles({
          corner,
          anchor: anchorOf(now.box, corner),
          aspect,
          bounds: now.bounds,
          obstacles,
        }),
      );
      setLimit((prev) => (prev && prev.min === min && prev.max === max ? prev : { min, max }));
    };
    fit();
    window.addEventListener('resize', fit);
    const container = target.current?.offsetParent;
    const observer =
      typeof ResizeObserver === 'undefined' || !(container instanceof HTMLElement)
        ? null
        : new ResizeObserver(fit);
    if (container instanceof HTMLElement) observer?.observe(container);
    return () => {
      window.removeEventListener('resize', fit);
      observer?.disconnect();
    };
  }, [width, resizing, target, corner, aspect, minWidthOf, getObstacles]);

  const finish = () => {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    setResizing(false);
    if (d.moved) onResizeEnd?.(d.lastW, d.minW);
  };

  const effectiveWidth = width === null ? null : limit ? clamp(width, limit.min, limit.max) : width;

  return {
    width: effectiveWidth,
    resizing,
    gripHandlers: {
      onPointerDown: (e: ReactPointerEvent<HTMLElement>) => {
        if (e.button !== 0) return;
        e.preventDefault();
        // 몸통의 끌어 옮기기나 클릭 열기로 전파되지 않게 막는다.
        e.stopPropagation();
        const el = target.current;
        const now = measure(el);
        const container = el?.offsetParent;
        if (!now || !(container instanceof HTMLElement)) return;
        e.currentTarget.setPointerCapture?.(e.pointerId);
        const minW = minWidthOf(now.bounds);
        const obstacles = getObstacles?.(container) ?? [];
        const maxW = Math.max(
          minW,
          maxWidthAgainstObstacles({
            corner,
            anchor: anchorOf(now.box, corner),
            aspect,
            bounds: now.bounds,
            obstacles,
          }),
        );
        setLimit({ min: minW, max: maxW });
        drag.current = {
          startX: e.clientX,
          startY: e.clientY,
          startBox: now.box,
          minW,
          maxW,
          lastW: now.box.width,
          moved: false,
        };
        setResizing(true);
      },
      onPointerMove: (e: ReactPointerEvent<HTMLElement>) => {
        const d = drag.current;
        if (!d) return;
        const raw = widthFromDrag(
          d.startBox.width,
          e.clientX - d.startX,
          e.clientY - d.startY,
          corner,
          aspect,
        );
        const nextW = clamp(raw, d.minW, d.maxW);
        d.lastW = nextW;
        d.moved = true;
        onResize(nextW, d.startBox);
      },
      onPointerUp: finish,
      onPointerCancel: finish,
      onLostPointerCapture: finish,
      onClick: (e: ReactMouseEvent<HTMLElement>) => {
        e.stopPropagation();
      },
      onDoubleClick: (e: ReactMouseEvent<HTMLElement>) => {
        e.stopPropagation();
        onReset?.();
      },
    },
  };
}
