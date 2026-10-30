import type { Bounds, PanelBox } from '@/features/events/panelBox';

export type Measured = { box: PanelBox; bounds: Bounds } | null;

/** 패널의 지금 자리와 부모(장면) 크기. */
export function measure(el: HTMLElement | null): Measured {
  const parent = el?.offsetParent;
  if (!el || !(parent instanceof HTMLElement)) return null;
  return {
    box: { left: el.offsetLeft, top: el.offsetTop, width: el.offsetWidth, height: el.offsetHeight },
    bounds: { width: parent.clientWidth, height: parent.clientHeight },
  };
}
