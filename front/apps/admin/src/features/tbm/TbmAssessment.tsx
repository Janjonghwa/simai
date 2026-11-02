import { SeverityBadge } from '@/features/events/SeverityBadge';
import { H3, NOTE } from '@/features/tbm/styles';
import { overlapsTime, type Interference } from '@/features/tbm/tbm';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import { TbmLegalGraph } from '@/features/tbm/TbmLegalGraph';
import {
  riskItemToEval,
  tbmFailureReasonText,
  type TbmReport,
  type TbmReportCitation,
  type TbmReportSection,
} from '@/features/tbm/types';
import { cn } from '@simai/ui/lib/utils';

// 작업 하나의 평가 결과 표 + 대책·법령 근거 + 법령 지식 그래프·단계 + 다른 작업과의 동시 작업 간섭 요약.

const EVAL_HEAD = 'px-1.5 py-2 text-left font-medium whitespace-nowrap text-faint';
const EVAL_CELL = 'border-t border-border px-1.5 py-2 text-left tabular-nums';
const EVAL_HEADS = ['항목', '규칙', '등급', '근거'];
const PAIR_ROW =
  'flex flex-wrap items-center gap-x-2.5 gap-y-1.5 rounded-lg px-3 py-2.5 text-[13px]';

const SECTION_KEYS_ORDER = ['hazards', 'measures', 'focus', 'legal'] as const;
const SECTION_FALLBACK_TITLES: Record<string, string> = {
  hazards: '잠재 위험요인',
  measures: '안전조치 대책',
  focus: '중점 위험 및 작업 전 확인',
  legal: '관련 법령 근거 요약',
};

function EvalTable({ task, report }: { task: TbmTask; report?: TbmReport | null }) {
  const rows =
    report && report.risk_items.length > 0 ? report.risk_items.map(riskItemToEval) : task.evals;

  if (!rows.length) return <p className={NOTE}>평가 항목 없음</p>;
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
        {rows.map((e, n) => (
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

function MeasuresAndLegalSection({ report }: { report: TbmReport | null | undefined }) {
  if (!report) {
    return <p className={NOTE}>생성된 TBM 보고서가 없습니다.</p>;
  }
  if (report.status === 'GENERATING') {
    return (
      <p className={NOTE}>
        보고서 생성 중입니다 (법령 후보 검색 → 지식 그래프 확장 → LLM 초안 → 인용 검증)…
      </p>
    );
  }
  if (report.status === 'FAILED') {
    return (
      <div
        role="alert"
        className="rounded-lg bg-destructive/15 px-3.5 py-2.5 text-[13px] text-destructive ring-1 ring-destructive/40 ring-inset"
      >
        {tbmFailureReasonText(report.failure_reason)}
      </div>
    );
  }

  const byKey = new Map<string, TbmReportSection>(report.sections.map((s) => [s.key, s]));
  const orderedSections: TbmReportSection[] = [];
  for (const key of SECTION_KEYS_ORDER) {
    const found = byKey.get(key);
    if (found) orderedSections.push(found);
  }
  for (const s of report.sections) {
    if (!SECTION_KEYS_ORDER.includes(s.key as (typeof SECTION_KEYS_ORDER)[number])) {
      orderedSections.push(s);
    }
  }

  const citationsById = new Map<string, TbmReportCitation>(
    report.citations.map((c) => [c.node_id, c]),
  );

  return (
    <div className="flex flex-col gap-3">
      {orderedSections.length === 0 ? (
        <p className={NOTE}>대책·법령 본문이 없습니다.</p>
      ) : (
        <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
          {orderedSections.map((sec) => (
            <div
              key={sec.key}
              data-testid={`report-section-${sec.key}`}
              className="flex flex-col gap-1.5 rounded-lg bg-surface-2 px-3 py-2.5 text-[12.5px] ring-1 ring-border ring-inset"
            >
              <div className="flex flex-wrap items-center justify-between gap-1.5">
                <strong className="text-[13px] text-foreground">
                  {sec.title || SECTION_FALLBACK_TITLES[sec.key] || sec.key}
                </strong>
                {sec.citations.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {sec.citations.map((cid) => {
                      const c = citationsById.get(cid);
                      return (
                        <span
                          key={cid}
                          className="rounded bg-amber-500/15 px-1.5 py-0.5 font-mono text-[11px] text-amber-300"
                        >
                          {c?.article_ref ?? cid}
                        </span>
                      );
                    })}
                  </div>
                )}
              </div>
              <p className="leading-relaxed whitespace-pre-line text-muted-foreground">
                {sec.body}
              </p>
            </div>
          ))}
        </div>
      )}

      {report.citations.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <h4 className="text-[12px] font-bold text-muted-foreground">인용 법령 조문</h4>
          <div className="flex flex-col gap-1.5">
            {report.citations.map((c) => (
              <div
                key={c.node_id}
                data-testid={`report-citation-${c.node_id}`}
                className="rounded-lg bg-surface-2 px-3 py-2 text-[12px] ring-1 ring-border ring-inset"
              >
                <div className="font-semibold text-foreground">
                  {c.source_name} {c.article_ref ? `${c.article_ref} ` : ''}· {c.title}
                </div>
                {c.excerpt && (
                  <p className="mt-0.5 leading-relaxed text-muted-foreground">{c.excerpt}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
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

/** 평가 결과 · 대책·법령 근거 · 법령 지식 그래프 · 동시 작업 간섭 구역. */
export function TbmAssessment({
  task,
  tasks,
  mine,
  report,
}: {
  task: TbmTask;
  tasks: readonly TbmTask[];
  mine: readonly Interference[];
  report?: TbmReport | null;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto">
      <section aria-label="평가 결과" className="flex flex-col gap-2">
        <h3 className={H3}>평가 결과</h3>
        <EvalTable task={task} report={report} />
      </section>

      <section aria-label="대책 · 법령 근거" className="flex flex-col gap-2">
        <h3 className={H3}>대책 · 법령 근거</h3>
        <MeasuresAndLegalSection report={report} />
      </section>

      {report && (
        <TbmLegalGraph
          subgraph={report.subgraph}
          citations={report.citations}
          steps={report.steps}
        />
      )}

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
