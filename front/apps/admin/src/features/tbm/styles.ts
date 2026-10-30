// 레이아웃 · TBM 보고서 화면이 같이 쓰는 클래스 조각.

/** 카드 안쪽 여백과 줄 간격. */
export const PANEL = 'flex-none gap-3 px-5 py-[18px]';
export const H2 = 'text-[15px] leading-[22px] font-bold text-muted-foreground';
export const H3 = 'text-[13px] font-bold text-muted-foreground';
export const NOTE = 'text-[13px] text-muted-foreground';
export const PILL =
  'rounded-full px-2.5 py-0.5 text-xs whitespace-nowrap text-muted-foreground ring-1 ring-faint ring-inset';
/** 카드 머리의 큰 단추(높이 38px). */
export const ACTION = 'h-[38px] px-3.5';
export const SECONDARY = `${ACTION} bg-surface-3`;
/** 작은 보조 단추(높이 30px): 보기 전환 · 쪽 넘김 · 하루치 묶음. */
export const SWITCH = 'h-[30px] bg-surface-2 px-3 text-[13px] text-muted-foreground';
/** 회의록 카드 폭: 카드 높이(100cqh)에서 머리 영역 110px 을 뺀 만큼 A4 세로 비(0.7071)로 맞추고 안쪽 여백 64px 을 더한다. */
export const MINUTES_WIDTH = 'w-[calc((100cqh-110px)*0.7071+64px)]';
