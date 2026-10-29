import { Button } from '@simai/ui/components/button';
import { Card } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import type { LucideIcon } from 'lucide-react';
import { useEffect, type ReactNode } from 'react';

const HEAD = 'flex min-h-[52px] w-full items-center gap-3 border-b border-border px-4 py-3';

/** 대시보드 카드. onOpen 을 주면 머리줄을 눌러 큰 창으로 전체 목록을 본다. */
export function DashPanel({
  icon: Icon,
  title,
  count,
  onOpen,
  children,
}: {
  icon: LucideIcon;
  title: string;
  count?: ReactNode;
  onOpen?: () => void;
  children: ReactNode;
}) {
  const name = (
    <span className="flex items-center gap-2 text-[15px] leading-[22px] font-bold">
      <Icon aria-hidden className="size-[18px] text-point-soft" />
      {title}
    </span>
  );
  const counter = count !== undefined && (
    <span className="ml-auto font-mono text-[13px] text-muted-foreground">{count}</span>
  );
  return (
    <Card className="min-h-0 min-w-0 gap-0 rounded-[12px] py-0 text-[15px] leading-[22px] ring-0">
      {onOpen ? (
        <h2 className="flex-none">
          <button
            type="button"
            aria-label={`${title} 전체 보기`}
            onClick={onOpen}
            className={cn(HEAD, 'cursor-pointer text-left hover:bg-surface-2')}
          >
            {name}
            {counter}
            <span
              aria-hidden
              className={cn(
                'text-[22px] leading-none text-muted-foreground',
                count === undefined ? 'ml-auto' : 'ml-2.5',
              )}
            >
              ›
            </span>
          </button>
        </h2>
      ) : (
        <div className={cn(HEAD, 'flex-none')}>
          <h2>{name}</h2>
          {counter}
        </div>
      )}
      {children}
    </Card>
  );
}

/** 대시보드 위에 뜨는 큰 창. 바깥을 누르거나 Esc 로 닫는다. */
export function DashModal({
  label,
  className,
  onClose,
  children,
}: {
  label: string;
  className?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      onClick={onClose}
      className="absolute inset-0 z-[8] flex items-center justify-center bg-foreground/28 p-5 dark:bg-background/62"
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label={label}
        onClick={(e) => e.stopPropagation()}
        className={cn(
          'flex max-h-full w-[min(860px,100%)] flex-col gap-2.5 rounded-[16px] bg-card px-[18px] py-4 shadow-float ring-1 ring-border ring-inset',
          className,
        )}
      >
        <div className="flex justify-end">
          <Button variant="secondary" size="icon-sm" aria-label="닫기" autoFocus onClick={onClose}>
            ✕
          </Button>
        </div>
        {children}
      </section>
    </div>
  );
}
