import { AlarmBanner } from '@/features/events/AlarmBanner';
import { AlarmSoundLoop } from '@/features/events/AlarmSoundLoop';
import { useEventsStore } from '@/features/events/events.store';
import { Dock } from '@/features/layout/Dock';
import { FailureBand } from '@/features/layout/FailureBand';
import { SpatialViewportHost } from '@/features/layout/SpatialViewportHost';
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
  const zoneEditor = useMatches({
    select: (matches) => matches.some((m) => m.staticData.zoneEditor),
  });
  const openEvent = useEventsStore((s) => s.openEvent);

  // 로그아웃으로 이 레이아웃을 벗어나면 연 이벤트를 닫는다. 다음 계정에 이전 화면 상태가 남지 않게 한다.
  useEffect(() => () => openEvent(null), [openEvent]);

  // 상단 바 아래 알림: 장애 띠, 그 아래 경보 배너. 실시간 관제처럼 화면을 채우는 화면은 장면 위에 띄우고
  // (이벤트 패널 자리는 비킨다), 다른 화면은 본문을 가리지 않게 본문 위에 자리를 차지한다. 알릴 것이 없으면 자리도 없다.
  const notices = (
    <div
      role="region"
      aria-label="알림"
      className={cn(
        'flex flex-col items-center gap-2 empty:hidden',
        fullBleed
          ? cn(
              'pointer-events-none absolute inset-x-5 top-[72px] z-[6]',
              eventPanel && 'right-[370px]',
            )
          : 'flex-none px-5 pt-3',
      )}
    >
      <FailureBand />
      <AlarmBanner withPanel={eventPanel} />
    </div>
  );

  return (
    <div className="relative flex h-svh min-h-[760px] min-w-[1280px] flex-col overflow-hidden bg-background text-[15px] leading-[22px]">
      <div className={cn('relative z-[7] flex-none', fullBleed && 'absolute inset-x-0 top-0')}>
        <TopBar floating={fullBleed} />
      </div>
      {!fullBleed && notices}
      <div className={cn('relative flex min-h-0 flex-1', fullBleed && 'absolute inset-0')}>
        {fullBleed ? (
          <SpatialViewportHost zoneEditor={zoneEditor}>
            <Outlet />
          </SpatialViewportHost>
        ) : (
          <main className="relative flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-auto px-5 pt-3.5 pb-[90px]">
            <Outlet />
          </main>
        )}
        <Dock />
      </div>
      {fullBleed && notices}
      <AlarmSoundLoop />
    </div>
  );
}
