import { AlarmBanner } from '@/features/events/AlarmBanner';
import { AlarmSoundLoop } from '@/features/events/AlarmSoundLoop';
import { useEventsStore } from '@/features/events/events.store';
import { Dock } from '@/features/layout/Dock';
import { TopBar } from '@/features/layout/TopBar';
import { cn } from '@simai/ui/lib/utils';
import { Outlet, useMatches } from '@tanstack/react-router';
import { useEffect } from 'react';

export function AppLayout() {
  const fullBleed = useMatches({
    select: (matches) => matches.some((m) => m.staticData.fullBleed),
  });
  const eventPanel = useMatches({
    select: (matches) => matches.some((m) => m.staticData.eventPanel),
  });
  const openEvent = useEventsStore((s) => s.openEvent);

  // 로그아웃으로 이 레이아웃을 벗어나면 연 이벤트를 닫는다. 다음 계정에 이전 화면 상태가 남지 않게 한다.
  useEffect(() => () => openEvent(null), [openEvent]);

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
      <AlarmBanner withPanel={eventPanel} />
      <AlarmSoundLoop />
    </div>
  );
}
