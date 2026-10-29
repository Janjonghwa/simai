import { LivePlan } from '@/features/plan/LivePlan';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useEffect, useRef, useState, type PointerEvent } from 'react';

// 3D 관제 왼쪽 아래 2D 미니맵(관제 데모와 같다). 누르면 큰 2D 창이 뜨고 그 창에서는 사람·구역을 고를 수 있다.
// 2D 는 3D 와 따로 그리므로(명세 13.6) 3D 가 멈춰도 같은 평면을 볼 수 있다.

const MIN_WIDTH = 180;
const MAX_WIDTH = 480;
const DEFAULT_WIDTH = 248;

export function PlanMinimap({
  selectedKey,
  onSelect,
}: {
  selectedKey: string | null;
  onSelect: (key: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const drag = useRef<{ x: number; width: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  // 놓거나 브라우저가 끌기를 거두면(pointercancel·캡처 잃음) 끝낸다. 남으면 누르지 않고 움직여도 폭이 바뀐다.
  const end = () => {
    drag.current = null;
    setDragging(false);
  };
  const grip = {
    onPointerDown: (e: PointerEvent<HTMLDivElement>) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.currentTarget.setPointerCapture?.(e.pointerId);
      drag.current = { x: e.clientX, width };
      setDragging(true);
    },
    onPointerMove: (e: PointerEvent<HTMLDivElement>) => {
      const d = drag.current;
      if (!d) return;
      setWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, d.width + e.clientX - d.x)));
    },
    onPointerUp: end,
    onPointerCancel: end,
    onLostPointerCapture: end,
  };

  return (
    <>
      <div
        data-testid="plan-minimap"
        style={{ width }}
        className="absolute bottom-24 left-4 z-[3] rounded-[14px] bg-card/82 shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset hover:ring-point-soft"
      >
        <button
          type="button"
          aria-label="2D 평면 크게 보기"
          onClick={() => setOpen(true)}
          className="block w-full cursor-zoom-in rounded-[14px] px-3 py-2.5"
        >
          {/* 미니맵 안에서는 고르지 않는다(버튼 안에 버튼을 두지 않는다). 고르기는 큰 창에서 한다. */}
          <div aria-hidden>
            <LivePlan selectedKey={selectedKey} className="aspect-square h-auto w-full" />
          </div>
        </button>
        {/* 오른쪽 위 모서리를 끌어 크기를 바꾼다. 두 번 누르면 처음 크기로 돌아간다. */}
        <div
          aria-hidden
          {...grip}
          onDoubleClick={() => setWidth(DEFAULT_WIDTH)}
          className={cn(
            'absolute -top-1.5 -right-1.5 z-[2] size-[18px] cursor-nesw-resize touch-none rounded-[6px] hover:bg-primary/35',
            'after:absolute after:inset-[5px] after:rounded-tr-[4px] after:border-t-2 after:border-r-2 after:border-faint',
            dragging && 'bg-primary/35',
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
