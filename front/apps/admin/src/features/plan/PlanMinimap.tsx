import {
  collectBottomPanelObstacles,
  defaultMinimapSize,
  useAspectResize,
} from '@/features/layout/useAspectResize';
import { useMonitorStandby } from '@/features/monitor/useTodayWorks';
import { LivePlan } from '@/features/plan/LivePlan';
import type { FloatBounds } from '@/lib/floatSpot';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

// 3D 관제 왼쪽 아래 2D 미니맵. 기본 크기는 칼럼 폭의 정사각형(--minimap-size)이고, 오른쪽 위 모서리 손잡이를 끌어
// 정사각형 비율을 유지한 채 크기를 조절할 수 있다(S15P31A101-829). 누르면 큰 2D 창이 뜨고 그 창에서는 사람·구역을 고를 수 있다.
// 2D 는 3D 와 따로 그리므로(명세 13.6) 3D 가 멈춰도 같은 평면을 볼 수 있다.

export const MINIMAP_SIZE_KEY = 'simai.plan.minimap.size.v1';

function readStoredSize(): number | null {
  try {
    const raw = localStorage.getItem(MINIMAP_SIZE_KEY);
    if (raw === null) return null;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? Math.round(value) : null;
  } catch {
    return null;
  }
}

function writeStoredSize(value: number | null) {
  try {
    if (value === null) localStorage.removeItem(MINIMAP_SIZE_KEY);
    else localStorage.setItem(MINIMAP_SIZE_KEY, String(Math.round(value)));
  } catch {
    /* 저장소를 쓸 수 없어도 기본 크기로 정상 동작한다. */
  }
}

export function PlanMinimap({
  selectedKey,
  onSelect,
}: {
  selectedKey: string | null;
  onSelect: (key: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [storedSize, setStoredSize] = useState<number | null>(readStoredSize);
  const box = useRef<HTMLDivElement>(null);
  // 대기 화면이 덮는 동안은 Tab 으로도 눌리지 않게 한다(이미 열어 둔 큰 창은 그대로 둔다).
  const standby = useMonitorStandby();

  const minWidthOf = useCallback((bounds: FloatBounds) => defaultMinimapSize(bounds.height), []);
  const getObstacles = useCallback(
    (container: HTMLElement) => collectBottomPanelObstacles(container, box.current),
    [],
  );
  const handleResize = useCallback((nextSize: number) => setStoredSize(nextSize), []);
  const handleResizeEnd = useCallback((finalSize: number, minSize: number) => {
    if (finalSize <= minSize) {
      setStoredSize(null);
      writeStoredSize(null);
      return;
    }
    setStoredSize(finalSize);
    writeStoredSize(finalSize);
  }, []);
  const handleReset = useCallback(() => {
    setStoredSize(null);
    writeStoredSize(null);
  }, []);

  const resize = useAspectResize({
    target: box,
    corner: 'top-right',
    aspect: 1,
    width: storedSize,
    minWidthOf,
    getObstacles,
    onResize: handleResize,
    onResizeEnd: handleResizeEnd,
    onReset: handleReset,
  });

  // 미니맵이 커지면 같은 부모 안의 왼쪽 구역 목록 칼럼 아래 끝(--side-column-bottom)도 올려 겹치지 않게 한다.
  // --minimap-size 자체는 바꾸지 않아 CCTV 창 기본 크기에 영향을 주지 않는다.
  useLayoutEffect(() => {
    const parent = box.current?.offsetParent;
    if (!(parent instanceof HTMLElement)) return undefined;
    if (resize.width === null) {
      parent.style.removeProperty('--side-column-bottom');
      return undefined;
    }
    parent.style.setProperty('--side-column-bottom', `calc(14px + ${resize.width}px + 8px)`);
    return () => {
      parent.style.removeProperty('--side-column-bottom');
    };
  }, [resize.width]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  return (
    <>
      <div
        ref={box}
        data-testid="plan-minimap"
        inert={standby}
        style={resize.width === null ? undefined : { width: resize.width, height: resize.width }}
        // 기본 크기는 CSS 변수(--minimap-size)에서 온다. 사용자가 크기를 바꾸면 인라인 정사각형 크기가 우선한다.
        className="absolute bottom-3.5 left-3.5 z-[3] size-(--minimap-size) rounded-[14px] bg-card/82 shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset hover:ring-point-soft"
      >
        <button
          type="button"
          aria-label="2D 평면 크게 보기"
          onClick={() => setOpen(true)}
          className="block size-full cursor-zoom-in rounded-[14px] p-2.5"
        >
          {/* 미니맵 안에서는 고르지 않는다(버튼 안에 버튼을 두지 않는다). 고르기는 큰 창에서 한다.
              작은 칸이라 방 전체 대신 구역·사람이 있는 곳에 맞춰 크게 비춘다(fit, -800). 큰 창은 방 전체다. */}
          <div aria-hidden className="size-full">
            <LivePlan selectedKey={selectedKey} fit className="size-full" />
          </div>
        </button>
        {/* 오른쪽 위(화면 안쪽) 모서리 손잡이: 정사각형 비율로 크기를 바꾸고, 두 번 누르면 기본 크기로 돌아간다. */}
        <div
          aria-hidden
          data-testid="plan-minimap-grip"
          data-grip="ne"
          title="끌어서 크기 조절 (두 번 누르면 기본 크기)"
          {...resize.gripHandlers}
          className={cn(
            'absolute -top-1.5 -right-1.5 z-[2] size-5 cursor-nesw-resize touch-none rounded-[6px] transition-colors',
            'hover:bg-primary/25',
            'after:absolute after:inset-[5px] after:rounded-tr-[4px] after:border-t-2 after:border-r-2 after:border-faint/70 after:transition-colors',
            'hover:after:border-primary',
            resize.resizing && 'bg-primary/35 after:border-primary',
          )}
        />
      </div>
      {open && (
        <div
          data-testid="plan-modal"
          onClick={() => setOpen(false)}
          className="absolute inset-0 z-[8] flex items-center justify-center bg-foreground/28 p-5 dark:bg-background/62"
        >
          <section
            role="dialog"
            aria-label="2D 평면"
            onClick={(e) => e.stopPropagation()}
            className="flex max-h-full w-[min(860px,100%)] flex-col gap-2.5 rounded-[16px] bg-card px-[18px] py-4 shadow-float ring-1 ring-border ring-inset"
          >
            <div className="flex justify-end">
              <Button
                variant="secondary"
                size="icon-sm"
                aria-label="닫기"
                onClick={() => setOpen(false)}
              >
                ✕
              </Button>
            </div>
            <div className="flex min-h-0 flex-1 justify-center">
              <LivePlan
                selectedKey={selectedKey}
                onSelect={onSelect}
                className="aspect-square h-auto w-[min(100%,600px,calc(100vh-320px))]"
              />
            </div>
          </section>
        </div>
      )}
    </>
  );
}
