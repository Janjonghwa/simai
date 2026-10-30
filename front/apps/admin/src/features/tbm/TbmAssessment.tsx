import { SeverityBadge } from '@/features/events/SeverityBadge';
import { H3, NOTE } from '@/features/tbm/styles';
import { overlapsTime, type Interference } from '@/features/tbm/tbm';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import { cn } from '@simai/ui/lib/utils';

// 작업 하나의 평가 결과 표와 다른 작업과의 동시 작업 간섭 요약.

const EVAL_HEAD = 'px-1.5 py-2 text-left font-medium whitespace-nowrap text-faint';
const EVAL_CELL = 'border-t border-border px-1.5 py-2 text-left tabular-nums';
const EVAL_HEADS = ['항목', '규칙', '등급', '근거'];
const PAIR_ROW =
  'flex flex-wrap items-center gap-x-2.5 gap-y-1.5 rounded-lg px-3 py-2.5 text-[13px]';

function EvalTable({ task }: { task: TbmTask }) {
  if (!task.evals.length) return <p className={NOTE}>평가 항목 없음</p>;
  return (
    <table className="w-full border-collapse text-[13px]">
      <thead>
        <tr>
          {EVAL_HEADS.map((head) => (
            <th key={head} scope="col" className={EVAL_HEAD}>
              {head}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {task.evals.map((e, n) => (
          <tr key={n}>
            <td className={EVAL_CELL}>{e.item}</td>
            <td className={cn(EVAL_CELL, 'font-mono whitespace-nowrap')}>{e.rule}</td>
            <td className={cn(EVAL_CELL, 'whitespace-nowrap')}>
              <SeverityBadge severity={e.sev} />
            </td>
            <td className={EVAL_CELL}>{e.why}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** 두 작업 사이의 간섭 한 줄. 간섭(hit)이 없으면 시간대가 겹치는지만 알린다. */
function PairRow({
  task,
  other,
  hit,
}: {
  task: TbmTask;
  other: TbmTask;
  hit: Interference | undefined;
}) {
  const pair = (
    <b>
      {task.id} ↔ {other.id}
    </b>
  );
  if (!hit)
    return (
      <div className={cn(PAIR_ROW, 'text-muted-foreground ring-1 ring-border ring-inset')}>
        {pair}
        <span>
          {overlapsTime(other, task)
            ? '같은 시간대 · 배치와 구역이 겹치지 않음'
            : '시간대 겹침 없음'}
        </span>
      </div>
    );
  return (
    <div className={cn(PAIR_ROW, 'bg-surface-2')}>
      {pair}
      <SeverityBadge severity={hit.sev} />
      <span className="text-muted-foreground">
        {hit.from}–{hit.to} 겹침
      </span>
      <span className="basis-full">{hit.why}</span>
    </div>
  );
}

/** 평가 결과와 동시 작업 간섭 두 구역. mine 은 이 작업이 낀 간섭이다. */
export function TbmAssessment({
  task,
  tasks,
  mine,
}: {
  task: TbmTask;
  tasks: readonly TbmTask[];
  mine: readonly Interference[];
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto">
      <section aria-label="평가 결과" className="flex flex-col gap-2">
        <h3 className={H3}>평가 결과</h3>
        <EvalTable task={task} />
      </section>
      <section aria-label="동시 작업 간섭" className="flex flex-col gap-2">
        <h3 className={H3}>동시 작업 간섭</h3>
        {tasks
          .filter((other) => other.id !== task.id)
          .map((other) => (
            <PairRow
              key={other.id}
              task={task}
              other={other}
              hit={mine.find((x) => x.a === other.id || x.b === other.id)}
            />
          ))}
      </section>
    </div>
  );
}
