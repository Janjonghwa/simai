import { isEmptyReplay, type EventRow } from '@/features/history/replayIndex';
import { ReplayTimeline, seekTo } from '@/features/history/ReplayPanels';
import { ReplayStage } from '@/features/history/ReplayStage';
import { useReplayStatus, useWorkReplay, type WorkReplay } from '@/features/history/useWorkReplay';
import { EventLog } from '@/features/history/WorkLists';
import { worksLoadingLine } from '@/features/history/works';
import type { HistoricalPersonNameSpan } from '@/features/people/personDisplay';
import { useHistoricalPersonNames } from '@/features/people/usePersonDisplay';
import type { ReplaySpatialSource } from '@/features/viewer3d';
import {
  dayWorksOf,
  firstMoment,
  isZeroSpanWork,
  pickRun,
  replayRunsOf,
  runChoiceLabel,
  runReplayWork,
  untouched,
  workChoiceLabel,
  ZERO_SPAN_REASON,
} from '@/features/work/past/pastDay';
import { PastReportPanel } from '@/features/work/past/PastReportPanel';
import type { PastDayProps } from '@/features/work/stageProps';
import type { WorkDayItem } from '@/features/work/types';
import { useWorkDays } from '@/features/work/useWorkDays';
import { formatDayTitle } from '@/features/work/workDay';
import type { WorkRun } from '@/lib/workRuns';
import { Button } from '@simai/ui/components/button';
import { useNavigate } from '@tanstack/react-router';
import { ChevronLeft } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

// 지난 날짜 다시 보기: 옛 히스토리의 다시 보기(타임라인·CCTV 다시 보기·이벤트 로그)에서 작업자 목록을 빼고 그날 TBM 보고서를 넣었다.
// 같은 부품(useWorkReplay·ReplayStage·ReplayTimeline·EventLog)을 그대로 쓴다. 오른쪽 열은 보고서와 이벤트 로그를 탭으로 나눈다.
// 하루 작업 안에서 실행을 여러 번 했으면(S15P31A101-696) 실행을 골라 그 실행 구간만 다시 본다(기본은 마지막 실행). 보고서는 하루 하나다.

const NO_EVENTS: readonly EventRow[] = [];

// 오른쪽 열은 560px. 좁은 화면에서는 한 열로 쌓고 오른쪽 열 높이를 따로 준다.
// 높이는 화면 높이로만 정한다: shrink-0 으로 안쪽(보고서·이벤트 로그 탭)의 내용이 높이를 바꾸지 못하게 하고, 행도 minmax(0,1fr) 로 고정해 두 탭 패널이 같은 크기가 된다.
const BODY =
  'grid shrink-0 grid-cols-[minmax(0,2.5fr)_minmax(320px,1fr)] gap-3.5 max-[1100px]:grid-cols-1';
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

/** 머리: 달력 단추 · 날짜 · 칩 `지난 작업` · (그날 작업이 둘 이상이면) 작업 선택기 · (실행이 둘 이상이면) 실행 선택기. */
function DayHead({
  date,
  works,
  pickedId,
  onPick,
  runs,
  pickedRun,
  onPickRun,
}: {
  date: string;
  works: readonly WorkDayItem[] | undefined;
  pickedId: string | undefined;
  onPick: (workId: string) => void;
  runs?: readonly WorkRun[];
  pickedRun?: number;
  onPickRun?: (runNo: number) => void;
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
      {runs && runs.length > 1 && onPickRun && (
        <span role="group" aria-label="실행 선택" className="flex flex-wrap gap-1.5">
          {runs.map((run) => (
            <button
              key={run.run_no}
              type="button"
              aria-pressed={run.run_no === pickedRun}
              onClick={() => onPickRun(run.run_no)}
              className={CHOICE}
            >
              {runChoiceLabel(run)}
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
  selectedEventId,
  personNames,
}: {
  selectedEventId: string | null;
  session: WorkReplay;
  personNames?: readonly HistoricalPersonNameSpan[];
  onPick: (event: EventRow) => void;
}) {
  const { playhead } = useReplayStatus(session.replay);
  return (
    <EventLog
      events={session.index?.events ?? NO_EVENTS}
      playhead={playhead}
      selectedEventId={selectedEventId}
      personNames={personNames}
      onPick={onPick}
    />
  );
}

function RightColumn({
  workId,
  session,
  onPickEvent,
  selectedEventId,
  personNames,
}: {
  workId: string;
  session: WorkReplay;
  onPickEvent: (event: EventRow) => void;
  selectedEventId: string | null;
  personNames?: readonly HistoricalPersonNameSpan[];
}) {
  return (
    <div className="flex min-h-0 flex-col gap-3 max-[1100px]:h-[760px]">
      <div className="flex max-h-[45%] min-h-0 flex-col overflow-hidden">
        <PastEvents
          session={session}
          onPick={onPickEvent}
          selectedEventId={selectedEventId}
          personNames={personNames}
        />
      </div>
      <div className="flex min-h-0 flex-1 flex-col *:flex-1">
        <PastReportPanel workId={workId} />
      </div>
    </div>
  );
}

export function PastDayPage({ date }: PastDayProps) {
  const { data: works, error } = useWorkDays({ from: date, to: date });
  const { data: personNames } = useHistoricalPersonNames(date);
  const dayWorks = useMemo(() => works && dayWorksOf(works, date), [works, date]);
  const [pickedId, setPickedId] = useState<string>();
  const [pickedRunNo, setPickedRunNo] = useState<number>();
  const work = dayWorks?.find((w) => w.work_id === pickedId) ?? dayWorks?.[0];
  const runs = useMemo(() => (work ? replayRunsOf(work) : []), [work]);
  const run = pickRun(runs, pickedRunNo);
  // 고른 실행의 구간만 다시 본다. 같은 실행이면 같은 객체라 다시 보기를 다시 만들지 않는다
  const replayWork = useMemo(() => work && runReplayWork(work, run), [work, run]);
  const session = useWorkReplay(replayWork);
  const { replay, index, range } = session;
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const pickEvent = useCallback(
    (event: EventRow) => {
      setSelectedEventId(event.eventId);
      seekTo(replay, Math.max(range?.from ?? event.raisedAt, event.raisedAt - 3000));
      replay?.play();
    },
    [replay, range],
  );

  // 처음 보일 시각: 첫 사건, 없으면 첫 기록. 긴 작업은 앞 구간만 받은 채 화면이 뜨고 뒤 구간에서 첫 사건이 나올 수 있어,
  // 우리가 옮겨 둔 자리를 사용자가 그대로 두는 동안에만 새 시각으로 옮긴다(옮기거나 재생했으면 그 자리를 지킨다).
  const start = useMemo(() => (index ? firstMoment(index) : null), [index]);
  const placed = useRef<{ replay: ReplaySpatialSource; at: number } | null>(null);
  useEffect(() => {
    if (!replay || start === null) return;
    const mine = placed.current?.replay === replay ? placed.current : null;
    if (mine && !untouched(replay, mine.at)) return;
    placed.current = { replay, at: start };
    seekTo(replay, start);
  }, [replay, start]);

  const head = (
    <DayHead
      date={date}
      works={dayWorks}
      pickedId={work?.work_id}
      onPick={(workId) => {
        setPickedId(workId);
        setPickedRunNo(undefined);
      }}
      runs={runs}
      pickedRun={run?.run_no}
      onPickRun={setPickedRunNo}
    />
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

  // 기록이 비었거나 다시 볼 구간이 없으면 타임라인을 두지 않고, 사유는 가운데 칸에만 한 번 보인다(S15P31A101-749).
  // 빈 기록(1초 미만·데이터 없음·잡힌 사람 없음)의 사유는 ReplayStage 가 가운데에 보인다.
  const emptyReplay = range !== null && index !== null && isEmptyReplay(index);
  const noRangeLine = isZeroSpanWork(replayWork)
    ? ZERO_SPAN_REASON
    : '아직 다시 볼 구간이 없습니다.';

  return (
    <div className="flex shrink-0 flex-col gap-3 pb-24">
      {head}
      <div className={BODY}>
        <section aria-label="CCTV 다시 보기" className="flex min-w-0 flex-col gap-3">
          <div className="aspect-video min-w-0">
            {range ? (
              <ReplayStage session={session} range={range} />
            ) : (
              <div
                role="status"
                className="flex h-full items-center justify-center rounded-lg border border-border bg-card p-6 text-muted-foreground"
              >
                {noRangeLine}
              </div>
            )}
          </div>
          {range && !emptyReplay && (
            <ReplayTimeline
              replay={replay}
              index={index}
              holdMs={session.holdMs}
              from={range.from}
              to={range.to}
              stepMs={session.stepMs}
              disabled={replay === null}
              onJump={pickEvent}
            />
          )}
        </section>
        <RightColumn
          workId={work.work_id}
          session={session}
          onPickEvent={pickEvent}
          selectedEventId={selectedEventId}
          personNames={personNames}
        />
      </div>
    </div>
  );
}
