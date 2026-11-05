import { SeverityBadge } from '@/features/events/SeverityBadge';
import { ALERT, H2, H3, NOTE, PANEL, SWITCH } from '@/features/tbm/styles';
import { TbmLegalGraph } from '@/features/tbm/TbmLegalGraph';
import {
  tbmFailureReasonText,
  type TbmReport,
  type TbmReportVersionSummary,
} from '@/features/tbm/types';
import { Button } from '@simai/ui/components/button';
import { Card } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';

// 가운데 블록: 법령 근거(RAG). 보고서가 없으면 보고서 작성 단추, 만드는 중이면 단계, 판이 있으면 최근 사건 반영 · 인용 조문 ·
// 법령 지식 그래프와 생성 · 검증 단계를 보인다. 머리 오른쪽에 판 고르기와 보고서 재작성 단추를 둔다.

function ZoneHistory({ report }: { report: TbmReport }) {
  const rows = report.zone_history.filter((h) => h.live_event_count > 0);
  if (!rows.length) return null;
  return (
    <section
      aria-label="최근 사건 반영"
      className="flex flex-col gap-1.5 rounded-lg bg-surface-2 px-3 py-2.5 ring-1 ring-border ring-inset"
    >
      <h3 className={H3}>최근 사건 반영</h3>
      {rows.map((h) => (
        <div key={h.zone_id} className="flex flex-wrap items-center gap-2 text-[13px]">
          <span className="font-mono">{h.zone_id}</span>
          <span className="text-muted-foreground">최근 {h.days ?? 7}일</span>
          {h.max_severity && <SeverityBadge severity={h.max_severity} />}
          <b className="font-mono">{h.live_event_count}건</b>
          {h.rules.length > 0 && (
            <span className="font-mono text-[12px] text-muted-foreground">
              {h.rules.join(' · ')}
            </span>
          )}
        </div>
      ))}
    </section>
  );
}

function CitationCards({ report }: { report: TbmReport }) {
  if (!report.citations.length) return <p className={NOTE}>인용 조문 없음</p>;
  return (
    <section aria-label="인용 법령 조문" className="flex flex-col gap-1.5">
      <h3 className={H3}>인용 법령 조문</h3>
      {report.citations.map((c, n) => (
        <div
          key={c.node_id}
          data-testid={`report-citation-${c.node_id}`}
          className="flex flex-col gap-1 rounded-lg bg-surface-2 px-3 py-2.5 text-[12.5px] shadow-[inset_3px_0_0_var(--color-primary)] ring-1 ring-border ring-inset"
        >
          <div className="flex flex-wrap items-baseline gap-2">
            <b className="font-mono text-point-soft">[{n + 1}]</b>
            <b className="text-foreground">
              {c.article_ref ? `${c.article_ref} ` : ''}
              {c.title}
            </b>
          </div>
          <span className="text-[12px] text-muted-foreground">{c.source_name}</span>
          {c.excerpt && <p className="leading-relaxed text-muted-foreground">{c.excerpt}</p>}
        </div>
      ))}
    </section>
  );
}

function GeneratingSteps({ report }: { report: TbmReport }) {
  if (!report.steps.length) return <p className={NOTE}>TBM 보고서 만드는 중…</p>;
  return (
    <ol aria-live="polite" className="flex flex-col gap-2">
      {report.steps.map((s, i) => (
        <li
          key={s.step}
          className={cn(
            'flex items-center gap-2.5 rounded-lg bg-surface-2 px-3 py-2.5 text-[13.5px] text-muted-foreground',
            s.status === 'DONE' && 'text-foreground',
            s.status === 'RUNNING' && 'text-foreground ring-1 ring-primary ring-inset',
          )}
        >
          <span
            aria-hidden
            className={cn(
              'inline-flex size-[22px] items-center justify-center rounded-full font-mono text-[12px] ring-1 ring-faint ring-inset',
              s.status === 'DONE' && 'bg-ok/15 text-ok ring-ok/60',
              s.status === 'RUNNING' && 'animate-pulse text-point-soft ring-primary',
            )}
          >
            {s.status === 'DONE' ? '✓' : i + 1}
          </span>
          {s.label}
        </li>
      ))}
    </ol>
  );
}

export function TbmLegalPanel({
  report,
  versions,
  selectedReportId,
  generating,
  onSelectVersion,
  onGenerate,
}: {
  report: TbmReport | null;
  versions: readonly TbmReportVersionSummary[];
  selectedReportId: number | null;
  generating: boolean;
  onSelectVersion: (reportId: number) => void;
  onGenerate: () => void;
}) {
  const busy = generating || report?.status === 'GENERATING';
  return (
    <Card role="region" aria-label="법령 근거" className={cn(PANEL, 'min-h-0 min-w-0 flex-1')}>
      <div className="flex flex-none items-center gap-2">
        <h2 className={cn(H2, 'whitespace-nowrap')}>법령 근거</h2>
        <span className="ml-auto" />
        {report && versions.length > 0 && (
          <select
            aria-label="보고서 버전"
            value={selectedReportId ?? versions[0]!.report_id}
            onChange={(e) => onSelectVersion(Number(e.target.value))}
            className="h-[30px] rounded-lg border border-border bg-surface-2 px-2 text-xs font-semibold text-foreground outline-none"
          >
            {versions.map((v) => (
              <option key={v.report_id} value={v.report_id}>
                v{v.report_version} ({v.status})
              </option>
            ))}
          </select>
        )}
        {report && (
          <Button
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={onGenerate}
            className={SWITCH}
          >
            보고서 재작성
          </Button>
        )}
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto pb-16">
        {!report && (
          <div className="flex flex-1 items-center justify-center">
            <Button disabled={busy} onClick={onGenerate} className="h-11 px-5 font-bold">
              {busy ? 'TBM 보고서 만드는 중…' : '보고서 작성'}
            </Button>
          </div>
        )}
        {report?.status === 'GENERATING' && <GeneratingSteps report={report} />}
        {report?.status === 'FAILED' && (
          <div role="alert" data-testid="tbm-report-failed" className={ALERT}>
            {tbmFailureReasonText(report.failure_reason)}
          </div>
        )}
        {report?.status === 'SUCCEEDED' && (
          <>
            <ZoneHistory report={report} />
            <CitationCards report={report} />
            <TbmLegalGraph
              subgraph={report.subgraph}
              citations={report.citations}
              steps={report.steps}
            />
          </>
        )}
      </div>
    </Card>
  );
}
