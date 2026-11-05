import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { formatClock, timeRange } from '@/features/history/works';
import { SIDE_PANEL_CLASS, SIDE_PANEL_TITLE_CLASS } from '@/features/monitor/sideColumnStyle';
import { useTodayWorks } from '@/features/monitor/useTodayWorks';
import { workEndedAt, workStartedAt, workZoneLabel } from '@/features/monitor/workSession';
import type { TbmWorkUnit } from '@/features/tbm/types';
import { fetchSetupLookup } from '@/features/tbm/useSetupLookup';
import { useWorkSession } from '@/features/tbm/useWorkSession';
import { confirmWorkEnd, confirmWorkStart } from '@/features/tbm/workConfirm';
import { deriveWorkStages, unfinishedBeforeStart } from '@/features/tbm/workStage';
import { Button } from '@simai/ui/components/button';
import { useQueryClient } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { useState } from 'react';

const STATUS_TEXT = { PLANNED: '예정', IN_PROGRESS: '진행 중', DONE: '종료' } as const;

/** 행의 시간 줄: 예정은 계획 시간대, 진행 중은 시작 시각, 끝난 작업은 실제 시간대. 시작 기록이 없으면 계획 시간대다. */
function timeLine(work: TbmWorkUnit): string {
  const started = workStartedAt(work);
  if (started === null) return timeRange(work.from_ms, work.to_ms);
  const ended = workEndedAt(work);
  return ended === null
    ? `시작 ${formatClock(started)}`
    : `${formatClock(started)}–${formatClock(ended)}`;
}

interface WorkRowProps {
  work: TbmWorkUnit;
  /** 시작·종료 버튼을 보일 수 있는 역할(관리자)인지. */
  canControl: boolean;
  /** 다른 작업이 진행 중이거나 요청 중이라 시작할 수 없는지(서버는 한 시점에 하나만 허용한다). */
  startBlocked: boolean;
  /** 요청 중이라 종료를 누를 수 없는지. */
  pending: boolean;
  /** 이 작업의 요청이 실패했을 때의 안내. */
  message: string;
  onStart: (work: TbmWorkUnit) => void;
  onEnd: (work: TbmWorkUnit) => void;
}

function WorkRow({
  work,
  canControl,
  startBlocked,
  pending,
  message,
  onStart,
  onEnd,
}: WorkRowProps) {
  const canStart = canControl && work.status === 'PLANNED';
  const canEnd = canControl && work.status === 'IN_PROGRESS';
  return (
    <li className="flex flex-col gap-0.5 rounded-[10px] bg-foreground/5 px-2.5 py-1.5">
      <div className="flex items-center gap-2">
        <b className="min-w-0 flex-1 truncate">{work.name}</b>
        {canStart && (
          <Button
            size="sm"
            className="shrink-0"
            disabled={startBlocked}
            onClick={() => onStart(work)}
          >
            작업 시작
          </Button>
        )}
        {canEnd && (
          <Button
            size="sm"
            variant="secondary"
            className="shrink-0"
            disabled={pending}
            onClick={() => onEnd(work)}
          >
            작업 종료
          </Button>
        )}
        {!canStart && !canEnd && work.status && (
          <span className="shrink-0 text-sm text-muted-foreground">{STATUS_TEXT[work.status]}</span>
        )}
      </div>
      <span className="truncate text-sm text-muted-foreground">
        <span className="tabular-nums">{timeLine(work)}</span> · {workZoneLabel(work)}
      </span>
      {message && (
        <p role="alert" className="text-sm text-muted-foreground">
          {message}
        </p>
      )}
    </li>
  );
}

/**
 * 실시간 관제의 오늘 작업 목록: 오늘 날짜 작업과 진행 중 작업. 관리자는 예정 작업을 시작하고 진행 중 작업을 종료한다
 * (종료하면 작업 관리 화면에서 마무리·서명). 반장·열람은 상태만 본다. 진행 중 작업이 있으면 다른 작업은 시작할 수 없다.
 * 받는 중이거나 오늘 작업이 없으면 그리지 않고(없음은 대기 화면이 보인다), 받지 못했으면 한 줄로 알린다.
 * 관제 왼쪽 칼럼에서 위험 구역 목록 아래에 놓이며, 칼럼이 모자라면 목록이 패널 안에서 스크롤한다.
 * 요청은 한 번에 하나라 시작·종료 세션과 세팅 조회는 목록에서 한 번만 쓰고, 안내는 마지막으로 누른 작업 행에 보인다.
 */
export function WorkListPanel() {
  const works = useTodayWorks();
  const canControl = useCurrentRole() === 'ADMIN';
  const client = useQueryClient();
  const navigate = useNavigate();
  const session = useWorkSession();
  const [actedId, setActedId] = useState<string | null>(null);
  // 세팅 세션을 받는 동안(확인창이 뜨기 전)에도 시작을 다시 누르지 못하게 한다. 두 번 누르면 확인창이 두 번 뜬다.
  const [checking, setChecking] = useState(false);

  if (works.state === 'loading' || (works.state === 'ready' && works.works.length === 0)) {
    return null;
  }

  const start = async (work: TbmWorkUnit) => {
    setChecking(true);
    const { setup, zonesCommittedHere } = await fetchSetupLookup(
      client,
      work.active_setup_session_id ?? null,
    ).finally(() => setChecking(false));
    const { stages } = deriveWorkStages({
      task: {
        status: work.status,
        actualStartedAt: work.actual_started_at,
        actualEndedAt: work.actual_ended_at,
      },
      setup,
      zonesCommittedHere,
      report: null,
      reportKnown: false,
    });
    if (!confirmWorkStart(unfinishedBeforeStart(stages))) return;
    setActedId(work.work_id);
    await session.run('start', work.work_id);
  };
  // 종료가 성공해야 작업 관리로 간다. 거기서 작업 마무리를 보고 서명을 받는다.
  const end = async (work: TbmWorkUnit) => {
    if (!confirmWorkEnd()) return;
    setActedId(work.work_id);
    if (await session.run('end', work.work_id)) {
      await navigate({ to: '/work', search: { workUnitId: work.work_id } });
    }
  };

  return (
    <section aria-label="오늘 작업" className={SIDE_PANEL_CLASS}>
      <h2 className={SIDE_PANEL_TITLE_CLASS}>오늘 작업</h2>
      {works.state === 'error' ? (
        <p role="alert" className="px-1.5 text-sm text-muted-foreground">
          작업 목록을 불러오지 못했습니다.
        </p>
      ) : (
        <ul className="flex min-h-0 flex-col gap-1.5 overflow-y-auto">
          {works.works.map((work) => (
            <WorkRow
              key={work.work_id}
              work={work}
              canControl={canControl}
              startBlocked={works.active !== null || session.pending || checking}
              pending={session.pending}
              message={actedId === work.work_id ? session.message : ''}
              onStart={(target) => void start(target)}
              onEnd={(target) => void end(target)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
