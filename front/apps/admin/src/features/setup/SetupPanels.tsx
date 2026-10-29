import { formatClock } from '@/features/setup/setup';
import {
  CHECKS,
  COVER_MIN_PCT,
  HEAD_ERR_MAX_CM,
  PAIR_MISMATCH,
  PROPS,
  RESULT,
  WALKERS,
  type SetupView,
  type Tone,
} from '@/features/setup/setupDemo';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import type { ReactNode } from 'react';

// 보정 화면 오른쪽 칸: 카메라 상태와 단계별 진행·결과.

const DOT: Record<Tone, string> = {
  ok: 'bg-ok',
  warn: 'bg-sev-caution',
  active: 'bg-primary',
  idle: 'bg-faint',
};

function Dot({ tone }: { tone: Tone }) {
  return <i aria-hidden className={cn('size-2 flex-none rounded-full', DOT[tone])} />;
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>{title}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">{children}</CardContent>
    </Card>
  );
}

function Rows({ children }: { children: ReactNode }) {
  return <ul className="divide-y divide-border">{children}</ul>;
}

function Row({ children }: { children: ReactNode }) {
  return <li className="flex items-center gap-2.5 px-0.5 py-2 text-sm">{children}</li>;
}

interface Stat {
  label: string;
  /** 아직 모르면 null. */
  value: string | null;
  ok?: boolean;
}

function Stats({ items }: { items: Stat[] }) {
  return (
    <dl className="grid grid-cols-2 gap-2.5">
      {items.map((item) => (
        <div key={item.label} className="grid gap-0.5 rounded-lg bg-surface-2 px-3 py-2.5">
          <dt className="text-xs text-muted-foreground">{item.label}</dt>
          <dd className={cn('font-mono text-[17px] font-semibold', item.ok && 'text-ok')}>
            {item.value ?? (
              <>
                <span aria-hidden>—</span>
                <span className="sr-only">값 없음</span>
              </>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function CameraPanel({ view }: { view: SetupView }) {
  return (
    <Panel title="카메라">
      <Rows>
        {view.cameras.map((row) => (
          <Row key={row.key}>
            <Dot tone={row.tone} />
            <b className="font-mono">{row.key}</b>
            <span className="text-muted-foreground">{row.label}</span>
            <span className="ml-auto font-mono">{row.value}</span>
          </Row>
        ))}
      </Rows>
    </Panel>
  );
}

function PropsPanel({ view }: { view: SetupView }) {
  return (
    <Panel title="실측 정물">
      <Rows>
        {PROPS.map((prop, n) => {
          const seen = view.propsSeen[n];
          return (
            <Row key={prop.name}>
              <Dot tone={seen ? 'ok' : 'warn'} />
              <span>{prop.name}</span>
              <span className="font-mono text-muted-foreground">{prop.height}</span>
              <span className="ml-auto">{seen ? '감지' : '1대에서만 보임'}</span>
            </Row>
          );
        })}
      </Rows>
    </Panel>
  );
}

function WalkPanel({ view }: { view: SetupView }) {
  const pct = Math.round(view.walkProgress * 100);
  return (
    <Panel title="보정 걷기">
      <div
        role="progressbar"
        aria-label="보정 걷기 진행"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        className="h-2 overflow-hidden rounded bg-surface-3"
      >
        <i className="block h-full bg-primary" style={{ width: `${pct}%` }} />
      </div>
      <Stats
        items={[
          { label: '경과', value: formatClock(view.walkSecs) },
          {
            label: `통로 관측 · 기준 ${COVER_MIN_PCT}%`,
            value: `${view.coverPct}%`,
            ok: view.coverOk,
          },
          { label: '실측 정물', value: `${PROPS.length} / ${PROPS.length} 일치` },
          { label: '걷는 사람', value: `${WALKERS}명` },
        ]}
      />
    </Panel>
  );
}

function CheckPanel({ view }: { view: SetupView }) {
  const { checked, maxErrorCm } = view;
  return (
    <Panel title={`보정 검증 · 머리 높이 오차 ${HEAD_ERR_MAX_CM}cm 이하`}>
      <Rows>
        {CHECKS.map((check, n) => {
          const done = n < checked;
          const cur = n === checked;
          return (
            <Row key={n}>
              <Dot tone={done ? 'ok' : cur ? 'active' : 'idle'} />
              <span>{n + 1}번 지점</span>
              <span className="ml-auto font-mono">
                {done ? `${check.errorCm}cm` : cur ? '측정 중' : '대기'}
              </span>
            </Row>
          );
        })}
      </Rows>
      <Stats
        items={[
          { label: '최대 오차', value: maxErrorCm === null ? null : `${maxErrorCm}cm` },
          { label: '카메라 쌍 불일치', value: checked ? PAIR_MISMATCH : null },
        ]}
      />
    </Panel>
  );
}

function ResultPanel({ view }: { view: SetupView }) {
  return (
    <Panel title="보정 결과">
      <Stats
        items={[
          { label: '재투영 중앙 · p95', value: RESULT.reprojection },
          { label: '통로 관측', value: `${view.coverPct}%` },
          {
            label: '머리 높이 오차',
            value: view.maxErrorCm === null ? null : `최대 ${view.maxErrorCm}cm`,
          },
          { label: '바닥 평면 차이', value: RESULT.floorDiff },
          { label: '스케일 근거', value: `실측 정물 ${PROPS.length}` },
          { label: '검증 지점', value: `${view.checked} / ${CHECKS.length}` },
        ]}
      />
    </Panel>
  );
}

/** 단계마다 다른 오른쪽 아래 칸. */
export function StagePanel({ view }: { view: SetupView }) {
  if (view.stage === 0) return <PropsPanel view={view} />;
  if (view.stage === 1) return <WalkPanel view={view} />;
  if (view.stage === 2) return <CheckPanel view={view} />;
  return <ResultPanel view={view} />;
}
