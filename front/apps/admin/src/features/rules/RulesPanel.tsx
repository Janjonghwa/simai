import { SEVERITY_LABEL } from '@/features/events/constants';
import type { Severity } from '@/features/events/types';
import { isTestProfile, ruleRows, type RuleRow } from '@/features/rules/rules';
import type { RulesResponse } from '@/features/rules/types';
import { useRules } from '@/features/rules/useRules';
import { Badge } from '@simai/ui/components/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { useMemo } from 'react';

// 규칙 현황(FR-3·113): 지금 현장이 판정에 쓰는 규칙 프로파일·구성 판과 R1~R10 의 활성 여부·판·등급·임계값. 읽기만 한다.

const NONE = '—';

const isSeverity = (value: string): value is Severity => value in SEVERITY_LABEL;

/** 기본 등급을 배지와 같은 한글 이름으로 보인다. 모르는 등급은 받은 글자 그대로. */
const severityText = (severity: string | undefined) => {
  if (severity === undefined) return NONE;
  return isSeverity(severity) ? SEVERITY_LABEL[severity] : severity;
};

/** 표 머리. 임계값 열은 남은 너비를 쓴다. */
const COLUMNS = [
  { label: '규칙', width: 'w-[28%]' },
  { label: '상태', width: 'w-[9%]' },
  { label: '판', width: 'w-[7%]' },
  { label: '기본 등급', width: 'w-[12%]' },
  { label: '임계값', width: undefined },
] as const;

const CELL = 'py-2';
const MONO_CELL = cn(CELL, 'font-mono');

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-mono">{value}</dd>
    </div>
  );
}

/** 지금 쓰는 프로파일과 구성 판. 발행만 하고 적용 전인 판이 있으면 함께 보인다. */
function RulesMeta({ rules }: { rules: RulesResponse }) {
  return (
    <dl className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1 text-sm font-normal">
      <Meta label="프로파일" value={rules.rule_profile} />
      <Meta label="구성 판" value={rules.config_version ?? NONE} />
      {rules.pending_config_version && (
        <Meta label="발행 대기 판" value={rules.pending_config_version} />
      )}
    </dl>
  );
}

/** 임계값 칸: 값이 있으면 이름과 값을, 자기 판이 없는 규칙은 함께 적용되는 규칙을 보인다. */
function ThresholdCell({ row }: { row: RuleRow }) {
  if (row.thresholds.length > 0) {
    return row.thresholds.map(({ key, value }) => (
      <span key={key} className="mr-3 inline-block">
        <span className="text-muted-foreground">{key}</span> {value}
      </span>
    ));
  }
  if (row.appliedWith.length > 0) return `${row.appliedWith.join(' · ')} 에 포함`;
  return NONE;
}

function RuleTableRow({ row }: { row: RuleRow }) {
  return (
    <tr className={cn('align-top', !row.active && 'text-muted-foreground')}>
      <th scope="row" className="py-2 pr-2 font-normal">
        <span className="font-mono font-semibold">{row.id}</span>
        {row.name && <span className="ml-2">{row.name}</span>}
      </th>
      <td className={CELL}>{row.active ? '활성' : '비활성'}</td>
      <td className={MONO_CELL}>{row.version ?? NONE}</td>
      <td className={MONO_CELL}>{severityText(row.severity)}</td>
      <td className={cn(MONO_CELL, 'text-xs leading-5')}>
        <ThresholdCell row={row} />
      </td>
    </tr>
  );
}

function RuleTable({ rows }: { rows: readonly RuleRow[] }) {
  return (
    <table className="w-full table-fixed border-collapse text-left text-sm">
      <thead>
        <tr className="border-b border-border text-xs text-muted-foreground">
          {COLUMNS.map(({ label, width }) => (
            <th key={label} scope="col" className={cn('py-1.5 font-normal', width)}>
              {label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-border">
        {rows.map((row) => (
          <RuleTableRow key={row.id} row={row} />
        ))}
      </tbody>
    </table>
  );
}

export function RulesPanel() {
  const { rules, loading, failed } = useRules();
  const rows = useMemo(() => (rules ? ruleRows(rules.rules) : []), [rules]);

  return (
    <Card size="sm" aria-label="규칙 현황" className="shrink-0">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2>규칙</h2>
          {rules && isTestProfile(rules) && (
            <Badge className="h-auto rounded-md bg-sev-caution/15 px-2 py-0.5 text-[13px] font-semibold text-sev-caution">
              시험 프로파일
            </Badge>
          )}
          {rules && <RulesMeta rules={rules} />}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {failed && (
          <p role="alert" className="text-sm text-muted-foreground">
            규칙을 받지 못했습니다
          </p>
        )}
        {loading && <p className="text-sm text-muted-foreground">{NONE}</p>}
        {rows.length > 0 && <RuleTable rows={rows} />}
      </CardContent>
    </Card>
  );
}
