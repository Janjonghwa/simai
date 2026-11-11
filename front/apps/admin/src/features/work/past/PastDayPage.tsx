import { isEmptyReplay, type EventRow } from '@/features/history/replayIndex';
import { ReplayTimeline, seekTo } from '@/features/history/ReplayPanels';
import { ReplayStage } from '@/features/history/ReplayStage';
import { useReplayStatus, useWorkReplay, type WorkReplay } from '@/features/history/useWorkReplay';
import { EventLog } from '@/features/history/WorkLists';
import { worksLoadingLine } from '@/features/history/works';
import { dayWorksOf, firstMoment, workChoiceLabel } from '@/features/work/past/pastDay';
import { PastReportPanel } from '@/features/work/past/PastReportPanel';
import type { PastDayProps } from '@/features/work/stageProps';
import type { WorkDayItem } from '@/features/work/types';
import { useWorkDays } from '@/features/work/useWorkDays';
import { formatDayTitle } from '@/features/work/workDay';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useNavigate } from '@tanstack/react-router';
import { ChevronLeft } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';

// 지난 날짜 다시 보기: 옛 히스토리의 다시 보기(타임라인·CCTV 다시 보기·이벤트 로그)에서 작업자 목록을 빼고 그날 TBM 보고서를 넣었다.
// 같은 부품(useWorkReplay·ReplayStage·ReplayTimeline·EventLog)을 그대로 쓴다. 오른쪽 열은 보고서와 이벤트 로그를 탭으로 나눈다.

type RightTab = 'report' | 'events';
const RIGHT_TABS: readonly { id: RightTab; label: string }[] = [
  { id: 'report', label: 'TBM 보고서' },
  { id: 'events', label: '이벤트 로그' },
];
const NO_EVENTS: readonly EventRow[] = [];

// 오른쪽 열은 560px. 좁은 화면에서는 한 열로 쌓고 오른쪽 열 높이를 따로 준다.
// 높이는 화면 높이로만 정한다: shrink-0 으로 안쪽(보고서·이벤트 로그 탭)의 내용이 높이를 바꾸지 못하게 하고, 행도 minmax(0,1fr) 로 고정해 두 탭 패널이 같은 크기가 된다.
const BODY =
  'grid h-[clamp(520px,calc(100vh-300px),900px)] shrink-0 grid-cols-[minmax(0,1fr)_560px] grid-rows-[minmax(0,1fr)] gap-3.5 max-[1100px]:h-auto max-[1100px]:grid-cols-1';
const CHIP =
  'inline-flex h-[22px] items-center rounded-full border border-border bg-surface-2 px-[9px] text-xs font-semibold whitespace-nowrap text-muted-foreground';
const CHOICE =
  'cursor-pointer rounded-full border border-border bg-card px-3 py-[5px] text-[13px] text-foreground hover:bg-surface-2 aria-pressed:border-primary aria-pressed:bg-primary aria-pressed:text-primary-foreground aria-pressed:hover:bg-primary aria-selected:border-primary aria-selected:bg-primary aria-selected:text-primary-foreground aria-selected:hover:bg-primary';

function BackButton() {
  const navigate = useNavigate();
  return (
    <Button variant="ghost" size="sm" onClick={() => void navigate({ to: '/work' })}>
      <ChevronLeft aria-hidden />
      달력
    </Button>
  );
}

/** 머리: 달력 단추 · 날짜 · 칩 `지난 작업` · (그날 작업이 둘 이상이면) 작업 선택기. */
function DayHead({
  date,
  works,
  pickedId,
  onPick,
}: {
  date: string;
  works: readonly WorkDayItem[] | undefined;
  pickedId: string | undefined;
  onPick: (workId: string) => void;
}) {
  return (
    <header className="flex flex-none flex-wrap items-center gap-x-3.5 gap-y-1">
      <BackButton />
      <h2 className="text-[17px] font-bold">{formatDayTitle(date)}</h2>
      <span className={CHIP}>지난 작업</span>
      {works && works.length > 1 && (
        <span role="group" aria-label="작업 선택" className="flex flex-wrap gap-1.5">
          {works.map((work) => (
            <button
              key={work.work_id}
              type="button"
              aria-pressed={work.work_id === pickedId}
              onClick={() => onPick(work.work_id)}
              className={CHOICE}
            >
              {workChoiceLabel(work)}
            </button>
          ))}
        </span>
      )}
    </header>
  );
}

/** 이벤트 로그 탭: 지금 재생 시각에 활성인 것을 강조하려고 재생 상태를 구독한다. */
function PastEvents({
  session,
  onPick,
}: {
  session: WorkReplay;
  onPick: (event: EventRow) => void;
}) {
  const { playhead } = useReplayStatus(session.replay);
  return (
    <EventLog events={session.index?.events ?? NO_EVENTS} playhead={playhead} onPick={onPick} />
  );
}

/** 오른쪽 열: `TBM 보고서 | 이벤트 로그` 탭. 두 패널을 모두 마운트해 두고 hidden 으로 전환한다. */
function RightColumn({
  workId,
  session,
  onPickEvent,
}: {
  workId: string;
  session: WorkReplay;
  onPickEvent: (event: EventRow) => void;
}) {
  const [tab, setTab] = useState<RightTab>('report');
  return (
    <div className="flex min-h-0 flex-col gap-2.5 max-[1100px]:h-[760px]">
      <span role="tablist" aria-label="오른쪽 보기" className="flex flex-wrap gap-1.5">
        {RIGHT_TABS.map(({ id, label }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={CHOICE}
          >
            {label}
          </button>
        ))}
      </span>
      <div
        role="tabpanel"
        aria-label="TBM 보고서"
        hidden={tab !== 'report'}
        className={cn(
          'min-h-0 flex-1 grid-rows-[minmax(0,1fr)]',
          tab === 'report' ? 'grid' : 'hidden',
        )}
      >
        <PastReportPanel workId={workId} />
      </div>
      <div
        role="tabpanel"
        aria-label="이벤트 로그"
        hidden={tab !== 'events'}
        className={cn(
          'min-h-0 flex-1 grid-rows-[minmax(0,1fr)]',
          tab === 'events' ? 'grid' : 'hidden',
        )}
      >
        <PastEvents session={session} onPick={onPickEvent} />
      </div>
    </div>
  );
}

/** 지난 날짜 화면. 그날 작업(둘 이상이면 고른 하나)의 구간을 다시 보고, 그 작업의 보고서·이벤트 로그를 옆에 둔다. */
export function PastDayPage({ date }: PastDayProps) {
  const { data: works, error } = useWorkDays({ from: date, to: date });
  const dayWorks = useMemo(() => works && dayWorksOf(works, date), [works, date]);
  const [pickedId, setPickedId] = useState<string>();
  const work = dayWorks?.find((w) => w.work_id === pickedId) ?? dayWorks?.[0];
  const session = useWorkReplay(work);
  const { replay, index, range } = session;
  const pickEvent = useCallback((event: EventRow) => seekTo(replay, event.raisedAt), [replay]);

  // 처음 보일 시각: 첫 사건, 없으면 첫 기록.
  const start = useMemo(() => (index ? firstMoment(index) : null), [index]);
  useEffect(() => {
    if (replay && start !== null) seekTo(replay, start);
  }, [replay, start]);

  const head = (
    <DayHead date={date} works={dayWorks} pickedId={work?.work_id} onPick={setPickedId} />
  );

  if (dayWorks === undefined) {
    return (
      <div className="flex min-h-0 flex-col gap-3 pb-24">
        {head}
        <p className="text-muted-foreground">{worksLoadingLine(error !== null)}</p>
      </div>
    );
  }
  if (!work) {
    return (
      <div className="flex min-h-0 flex-col gap-3 pb-24">
        {head}
        <p className="text-muted-foreground">기록된 작업이 없습니다.</p>
      </div>
    );
  }

  return (
    <div className="flex shrink-0 flex-col gap-3 pb-24">
      {head}
      {range ? (
        <ReplayTimeline
          replay={replay}
          index={index}
          holdMs={session.holdMs}
          from={range.from}
          to={range.to}
          stepMs={session.stepMs}
          disabled={replay === null || (index !== null && isEmptyReplay(index))}
          onJump={pickEvent}
        />
      ) : (
        <p className="text-muted-foreground">아직 다시 볼 구간이 없습니다.</p>
      )}
      <div className={BODY}>
        <section aria-label="CCTV 다시 보기" className="flex min-h-[420px] min-w-0 flex-col">
          {range && <ReplayStage session={session} range={range} />}
        </section>
        <RightColumn workId={work.work_id} session={session} onPickEvent={pickEvent} />
      </div>
    </div>
  );
}
