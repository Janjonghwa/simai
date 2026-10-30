import { isEmptyReplay, type EventRow } from '@/features/history/replayIndex';
import { ReplayLists, ReplayTimeline, seekTo } from '@/features/history/ReplayPanels';
import { ReplayStage } from '@/features/history/ReplayStage';
import type { WorkUnit } from '@/features/history/types';
import { useWorkReplay, useWorks } from '@/features/history/useWorkReplay';
import { formatDate, timeRange, worksLoadingLine } from '@/features/history/works';
import { PageTitle } from '@/features/layout/PageTitle';
import type { Viewer3DHandle, ViewerTarget } from '@/features/viewer3d';
import { useZones } from '@/features/zones/useZones';
import { Button } from '@simai/ui/components/button';
import { useNavigate } from '@tanstack/react-router';
import { ChevronLeft } from 'lucide-react';
import { useCallback, useRef, useState } from 'react';

// 화면 틀: 가운데 칸(CCTV·작은 창)과 오른쪽 열(작업자·이벤트 로그, 420px).
const BODY = 'grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_420px] gap-3.5';
const STAGE = 'relative flex min-h-[420px] min-w-0 flex-col';

function BackButton() {
  const navigate = useNavigate();
  return (
    <Button variant="ghost" size="sm" onClick={() => void navigate({ to: '/history' })}>
      <ChevronLeft aria-hidden />
      작업 목록
    </Button>
  );
}

function WorkHeader({ work }: { work: WorkUnit }) {
  return (
    <header className="flex flex-none flex-wrap items-center gap-x-4 gap-y-1">
      <BackButton />
      <h2 className="text-[17px] font-bold">{work.name}</h2>
      <span className="text-muted-foreground">
        {formatDate(work.date)} ·{' '}
        <span className="font-mono">{timeRange(work.from_ms, work.to_ms)}</span>
        {' · '}
        {work.place ?? '—'}
      </span>
    </header>
  );
}

/** 작업 다시 보기: 작업 구간의 CCTV(목 화면)·3D(재생)·타임라인·작업자·이벤트 로그. 작업자는 익명 World ID 다. */
export function WorkReplayPage({ workId }: { workId: string }) {
  const { works, failed: worksFailed } = useWorks();
  const work = works?.find((w) => w.work_id === workId);
  const { raw: zonesRaw } = useZones();
  const session = useWorkReplay(work, zonesRaw);
  const viewer = useRef<Viewer3DHandle>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const { replay, index } = session;

  const pickWorker = useCallback((worldId: string) => {
    const target: ViewerTarget = { type: 'person', id: worldId };
    setSelected(worldId);
    viewer.current?.select(target);
    viewer.current?.focus(target);
  }, []);
  const pickEvent = useCallback(
    (event: EventRow) => {
      seekTo(replay, event.raisedAt);
      const target = event.worldIds[0];
      if (target) pickWorker(target);
    },
    [replay, pickWorker],
  );
  const select3d = useCallback(
    (target: ViewerTarget | null) => setSelected(target?.type === 'person' ? target.id : null),
    [],
  );

  if (!work) {
    return (
      <div className="flex flex-col items-start gap-3">
        <PageTitle>작업 다시 보기</PageTitle>
        <BackButton />
        <p className="text-muted-foreground">
          {works === undefined ? worksLoadingLine(worksFailed) : '작업을 찾을 수 없습니다.'}
        </p>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <PageTitle>작업 다시 보기</PageTitle>
      <WorkHeader work={work} />
      <ReplayTimeline
        replay={replay}
        index={index}
        holdMs={session.holdMs}
        from={work.from_ms}
        to={work.to_ms}
        stepMs={session.stepMs}
        disabled={replay === null || (index !== null && isEmptyReplay(index))}
        onJump={pickEvent}
      />
      <div className={BODY}>
        <section aria-label="CCTV 다시 보기" className={STAGE}>
          <ReplayStage
            session={session}
            range={{ from: work.from_ms, to: work.to_ms }}
            selectedId={selected}
            viewerRef={viewer}
            onPickWorker={pickWorker}
            onSelectionChange={select3d}
          />
        </section>
        <ReplayLists
          replay={replay}
          index={index}
          holdMs={session.holdMs}
          planned={work.planned_people}
          selectedId={selected}
          onPickWorker={pickWorker}
          onPickEvent={pickEvent}
        />
      </div>
    </div>
  );
}
