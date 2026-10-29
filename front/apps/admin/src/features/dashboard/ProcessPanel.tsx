import { DashPanel } from '@/features/dashboard/DashPanel';
import {
  barPercent,
  planDiff,
  processSummary,
  type ProcessRow,
  type ProcessStatus,
} from '@/features/dashboard/dashboard';
import { DEMO_PROCESS } from '@/features/dashboard/dashboardDemo';
import { useFit } from '@/features/dashboard/useFit';
import { Badge } from '@simai/ui/components/badge';
import { cn } from '@simai/ui/lib/utils';
import { Layers } from 'lucide-react';
import type { Ref } from 'react';

/** 카드에 보이는 공정 수(넘치면 2개까지 줄인다). */
const PROCESS_ROWS = 3;

// 지연만 주황(CONVENTION). 진행 중은 강조색, 정상은 초록이다.
const STATUS_TONE: Record<ProcessStatus, string> = {
  '진행 중': 'border-primary text-point-soft',
  정상: 'border-ok/60 text-ok',
  '지연 확인': 'border-delay text-delay-soft',
};

const ROW =
  'grid min-h-11 grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)_52px_84px_82px] items-center gap-3 border-b border-border text-sm';

/** 계획 = 옅은 바탕 막대 + 세로 표시선, 실적 = 진한 채움 막대. */
export function PlanBar({
  plan,
  actual,
  small,
}: {
  plan: number;
  actual: number;
  small?: boolean;
}) {
  const planWidth = `${barPercent(plan)}%`;
  return (
    <span
      role="img"
      aria-label={`계획 ${plan}% · 실적 ${actual}%`}
      className={cn(
        'relative block w-full bg-surface-3',
        small ? 'h-2 max-w-20 rounded-[2px]' : 'h-3 rounded-[3px]',
      )}
    >
      <span
        className="absolute inset-y-0 left-0 rounded-[inherit] bg-muted-foreground/30"
        style={{ width: planWidth }}
      />
      <span
        className={cn(
          'absolute left-0 rounded-[2px] bg-primary',
          small ? 'inset-y-0.5' : 'inset-y-[3px]',
        )}
        style={{ width: `${barPercent(actual)}%` }}
      />
      <span
        className="absolute inset-y-[-3px] -ml-px w-0.5 rounded-[1px] bg-muted-foreground"
        style={{ left: planWidth }}
      />
    </span>
  );
}

function ProcessLine({ row, hidden }: { row: ProcessRow; hidden: boolean }) {
  return (
    <div role="row" className={cn(ROW, hidden && 'invisible')}>
      <strong role="cell" className="truncate font-medium">
        {row.task} · {row.name}
      </strong>
      <span role="cell" className="text-muted-foreground">
        {row.place}
      </span>
      <span role="cell" className="text-right font-mono text-muted-foreground">
        {row.plan}%
      </span>
      <span role="cell" className="grid justify-items-end gap-1 font-mono">
        <span className="font-semibold text-primary">{row.actual}%</span>
        <PlanBar plan={row.plan} actual={row.actual} small />
      </span>
      <span role="cell">
        <Badge
          variant="outline"
          className={cn('rounded-[4px] font-semibold', STATUS_TONE[row.status])}
        >
          {row.status}
        </Badge>
      </span>
    </div>
  );
}

/** 공정 표. 카드에서는 fitRef 로 줄 칸 높이를 재어 visible 줄만 보인다. */
export function ProcessTable({
  rows,
  fitRef,
  visible = rows.length,
  className,
  bodyClassName,
}: {
  rows: readonly ProcessRow[];
  fitRef?: Ref<HTMLDivElement>;
  visible?: number;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <div role="table" aria-label="공정 현황" className={cn('flex min-h-0 flex-col', className)}>
      <div role="row" className={cn(ROW, 'min-h-8 text-xs text-muted-foreground')}>
        <span role="columnheader">공정명</span>
        <span role="columnheader">구역</span>
        <span role="columnheader" className="text-right">
          계획
        </span>
        <span role="columnheader" className="text-right">
          실적
        </span>
        <span role="columnheader">상태</span>
      </div>
      <div role="rowgroup" ref={fitRef} className={bodyClassName}>
        {rows.map((row, n) => (
          <ProcessLine key={row.task} row={row} hidden={n >= visible} />
        ))}
      </div>
    </div>
  );
}

export function ProcessPanel({ onOpen }: { onOpen: () => void }) {
  const summary = processSummary(DEMO_PROCESS);
  const diff = summary && planDiff(summary.plan, summary.actual);
  const shown = DEMO_PROCESS.slice(0, PROCESS_ROWS);
  const [fitRef, visible] = useFit<HTMLDivElement>(
    PROCESS_ROWS,
    shown.map((row) => row.task).join(),
  );

  return (
    <DashPanel icon={Layers} title="공정 현황" onOpen={onOpen}>
      <div className="flex min-h-0 flex-1 flex-col gap-3 px-4 py-3.5">
        {summary && (
          <div className="grid flex-none grid-cols-[170px_minmax(0,1fr)] gap-6 border-b border-border pb-3.5">
            <div>
              <span className="text-[13px] text-muted-foreground">전체 공정 진행률</span>
              <strong className="block font-mono text-[34px] leading-[42px] font-semibold">
                {summary.actual}
                <small className="ml-1 font-sans text-[13px] font-medium text-muted-foreground">
                  %
                </small>
              </strong>
            </div>
            <div className="flex flex-col justify-center gap-2.5">
              <div className="flex items-baseline gap-3.5 text-[13px]">
                <span className="text-muted-foreground">계획 {summary.plan}%</span>
                <span className="font-semibold text-primary">실적 {summary.actual}%</span>
                {diff && (
                  <em
                    className={cn(
                      'ml-auto text-xs not-italic',
                      diff.behind ? 'text-delay-soft' : 'text-muted-foreground',
                    )}
                  >
                    {diff.text}
                  </em>
                )}
              </div>
              <PlanBar plan={summary.plan} actual={summary.actual} />
            </div>
          </div>
        )}
        <ProcessTable
          rows={shown}
          fitRef={fitRef}
          visible={visible}
          className="flex-1"
          bodyClassName="relative min-h-0 flex-1 overflow-hidden"
        />
      </div>
    </DashPanel>
  );
}
