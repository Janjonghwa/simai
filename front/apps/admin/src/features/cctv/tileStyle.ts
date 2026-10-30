// CCTV 칸(영상 칸·모의 칸·상태 칸)이 함께 쓰는 클래스.

/** 칸 틀. 테두리는 그림 위에 겹쳐 그린다(안쪽 그림자는 그림에 가린다). */
export const TILE_FRAME =
  'relative size-full overflow-hidden rounded-lg outline-none after:pointer-events-none after:absolute after:inset-0 after:rounded-[inherit] after:ring-1 after:ring-border after:ring-inset focus-visible:ring-3 focus-visible:ring-ring/50';

/** 누르면 크게 보는 칸의 커서·테두리. 크게 보는 칸은 굵은 테두리를 두른다. */
export const tileZoom = (focused: boolean) =>
  focused ? 'cursor-zoom-out after:ring-2 after:ring-primary' : 'cursor-zoom-in';

/** 영상이 지금 것이 아닐 때 흐리게 보인다. */
export const DIMMED = 'opacity-45';

/** LIVE 점. */
export const LIVE_DOT = 'size-[7px] rounded-full bg-sev-critical';
