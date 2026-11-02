import { useCamSplitStore } from '@/features/cctv/camSplit.store';
import { AlarmBanner } from '@/features/events/AlarmBanner';
import { AlarmSoundLoop } from '@/features/events/AlarmSoundLoop';
import { useEventsStore } from '@/features/events/events.store';
import { noticeInset } from '@/features/events/panelBox';
import { Dock } from '@/features/layout/Dock';
import { FailureBand } from '@/features/layout/FailureBand';
import { SpatialViewportHost } from '@/features/layout/SpatialViewportHost';
import { TopBar } from '@/features/layout/TopBar';
import { useHealthSnapshot } from '@/features/layout/useHealthSnapshot';
import { cn } from '@simai/ui/lib/utils';
import { Outlet, useMatches } from '@tanstack/react-router';
import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';

/**
 * 장면 위에 뜬 알림(장애 띠·경보 배너)이 차지하는 높이를 `--notices-h` 로 알린다(없으면 0).
 * 장면 위 큰 창(사건 상세)이 알림을 가리지 않고 그 아래에서 시작하게 한다.
 */
function useNoticesHeight(
  notices: RefObject<HTMLDivElement | null>,
  root: RefObject<HTMLDivElement | null>,
) {
  useLayoutEffect(() => {
    const el = notices.current;
    const host = root.current;
    if (!el || !host || typeof ResizeObserver === 'undefined') return undefined;
    const update = () => {
      // 상단 바 아래 12px 틈에서 시작하므로, 알림이 있으면 그 틈과 아래 8px 틈을 더한다.
      const h = el.offsetHeight;
      host.style.setProperty('--notices-h', h > 0 ? `${h + 20}px` : '0px');
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [notices, root]);
}

/**
 * 상단 바 아래 알림: 장애 띠, 그 아래 경보 배너. 모든 화면에서 내용 위에 띄우고 본문을 밀거나 줄이지 않는다
 * (관제 데모와 같다). 실시간 관제는 이벤트 패널 자리를 비킨다(옮기면 패널 양옆 가운데 넓은 쪽). 알릴 것이 없으면 숨는다.
 * 패널을 끄는 동안 이 부분만 다시 그린다.
 */
function Notices({
  ref,
  eventPanel,
}: {
  ref: RefObject<HTMLDivElement | null>;
  eventPanel: boolean;
}) {
  const span = useEventsStore((s) => (eventPanel ? s.panelSpan : null));
  // 카메라 미리보기가 오른쪽에 열려 있으면 그 폭(CamSplitPanel)만큼 더 비킨다.
  const camSplit = useCamSplitStore((s) => eventPanel && s.split !== null);
  return (
    <div
      ref={ref}
      role="region"
      aria-label="알림"
      style={
        span
          ? noticeInset(span)
          : camSplit
            ? { right: 'calc(clamp(340px, 34%, 560px) + 30px)' }
            : undefined
      }
      className={cn(
        'pointer-events-none absolute inset-x-5 top-[72px] z-[6] flex flex-col items-center gap-2 empty:hidden',
        eventPanel && !span && 'right-[370px]',
      )}
    >
      <FailureBand />
      <AlarmBanner withPanel={eventPanel} />
    </div>
  );
}

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
  const root = useRef<HTMLDivElement>(null);
  const noticesRef = useRef<HTMLDivElement>(null);
  useNoticesHeight(noticesRef, root);
  // 연결이 (다시) 열리면 /topic/health 를 기다리지 않고 지금 상태를 먼저 채운다(GET /api/health).
  useHealthSnapshot();

  // 로그아웃으로 이 레이아웃을 벗어나면 연 이벤트를 닫는다. 다음 계정에 이전 화면 상태가 남지 않게 한다.
  useEffect(() => () => openEvent(null), [openEvent]);

  return (
    <div
      ref={root}
      className="relative flex h-svh min-h-[760px] min-w-[1280px] flex-col overflow-hidden bg-background text-[15px] leading-[22px]"
    >
      <div className={cn('relative z-[7] flex-none', fullBleed && 'absolute inset-x-0 top-0')}>
        <TopBar floating={fullBleed} />
      </div>
      <div className={cn('relative flex min-h-0 flex-1', fullBleed && 'absolute inset-0')}>
        {fullBleed ? (
          <SpatialViewportHost zoneEditor={zoneEditor} eventPanel={eventPanel}>
            <Outlet />
          </SpatialViewportHost>
        ) : (
          <main className="relative flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-auto bg-blueprint px-5 pt-3.5 pb-[90px]">
            <Outlet />
          </main>
        )}
        <Dock />
      </div>
      <Notices ref={noticesRef} eventPanel={eventPanel} />
      <AlarmSoundLoop />
    </div>
  );
}
