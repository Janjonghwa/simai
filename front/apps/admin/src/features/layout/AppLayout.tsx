import { Dock } from '@/features/layout/Dock';
import { TopBar } from '@/features/layout/TopBar';
import { cn } from '@simai/ui/lib/utils';
import { Outlet, useMatches } from '@tanstack/react-router';

export function AppLayout() {
  const fullBleed = useMatches({
    select: (matches) => matches.some((m) => m.staticData.fullBleed),
  });

  return (
    <div className="relative flex h-svh min-h-[760px] min-w-[1280px] flex-col overflow-hidden bg-background text-[15px] leading-[22px]">
      <div className={cn('relative z-[7] flex-none', fullBleed && 'absolute inset-x-0 top-0')}>
        <TopBar floating={fullBleed} />
      </div>
      <div className={cn('relative flex min-h-0 flex-1', fullBleed && 'absolute inset-0')}>
        {fullBleed ? (
          <Outlet />
        ) : (
          <main className="relative flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-auto px-5 pt-3.5 pb-[90px]">
            <Outlet />
          </main>
        )}
        <Dock />
      </div>
    </div>
  );
}
