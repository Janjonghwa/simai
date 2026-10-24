import { PageTitle } from '@/features/layout/PageTitle';
import {
  calibrationLabel,
  chipState,
  configLabel,
  dataAgeSec,
  sceneLabel,
  sceneMismatch,
} from '@/features/layout/status';
import { SystemHealthChip } from '@/features/layout/SystemHealthChip';
import { useNow } from '@/features/layout/useNow';
import {
  cameraRows,
  connectionRows,
  latencyRows,
  processingRows,
  type StatusRow,
} from '@/features/system/health';
import { useMetrics } from '@/features/system/useMetrics';
import { useLinkAlive } from '@/hooks/useLinkAlive';
import { useRealtimeStore } from '@/store/realtime.store';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import type { ReactNode } from 'react';

// 시스템 상태(명세 3.11, FR-13). 실시간 health 와 지표 API 를 보인다. 연결이 끊기면 지금 값을 모르므로 비운다
// (상단 바와 같다). 지연은 주황(delay)으로만 보이고 위험 등급 색은 쓰지 않는다.
// health 는 칸마다 따로 구독해, 바뀐 칸만 다시 그린다.

/** 끊기면 지금 값을 모른다. 마지막 값을 지금 값처럼 보이지 않는다. */
const useCurrentHealth = (alive: boolean) => useRealtimeStore((s) => (alive ? s.health : null));

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function Blank() {
  return (
    <>
      <span aria-hidden>—</span>
      <span className="sr-only">값 없음</span>
    </>
  );
}

function Value({
  value,
  delay,
  warn,
  mono = true,
}: {
  value?: string;
  delay?: boolean;
  warn?: boolean;
  mono?: boolean;
}) {
  if (value === undefined) return <Blank />;
  return (
    <span
      className={cn(
        mono && 'font-mono tabular-nums',
        delay && 'text-delay',
        warn && 'rounded bg-stripes px-1',
      )}
    >
      {value}
    </span>
  );
}

function Rows({
  rows,
  mono,
  className,
}: {
  rows: StatusRow[];
  mono?: boolean;
  className?: string;
}) {
  return (
    <dl className={cn('grid grid-cols-[auto_1fr] gap-x-3 gap-y-1', className)}>
      {rows.map((row) => (
        <div key={row.key} className="contents">
          <dt className="text-muted-foreground">{row.label}</dt>
          <dd>
            <Value value={row.value} delay={row.delay} warn={row.warn} mono={mono} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

const HEAD = 'px-2 py-1.5 text-left text-xs font-medium text-muted-foreground';
const CELL = 'border-t border-border px-2 py-1.5';
const ROW_HEAD = cn(CELL, 'text-left font-normal');

function DataAge({ alive }: { alive: boolean }) {
  const now = useNow();
  const age = useRealtimeStore((s) => dataAgeSec(s.health, now));
  return <Value value={age === undefined ? undefined : `${age.toFixed(1)}초`} delay={!alive} />;
}

function Overview({ alive }: { alive: boolean }) {
  const state = useRealtimeStore((s) => chipState(s, alive));
  const health = useCurrentHealth(alive);
  return (
    <div className="grid grid-cols-4 gap-3">
      <Section title="System Health">
        {/* 상단 바의 칩이 바뀜을 알린다. 여기서 또 알리지 않는다. */}
        <SystemHealthChip state={state} live={false} />
      </Section>
      <Section title="연결">
        <Rows rows={connectionRows(health)} mono={false} />
      </Section>
      <Section title="데이터 경과">
        <DataAge alive={alive} />
      </Section>
      <Section title="보정 · 구성">
        <Rows
          rows={[
            { key: 'calibration', label: '보정', value: calibrationLabel(health) },
            { key: 'config', label: '구성', value: configLabel(health) },
            {
              key: 'scene',
              label: '장면',
              value: sceneLabel(health),
              warn: sceneMismatch(health) !== undefined,
            },
          ]}
        />
      </Section>
    </div>
  );
}

function Cameras({ alive }: { alive: boolean }) {
  const rows = cameraRows(useCurrentHealth(alive));
  return (
    <Section title="카메라">
      <table aria-label="카메라" className="w-full text-sm">
        <thead>
          <tr>
            <th scope="col" className={HEAD}>
              카메라
            </th>
            <th scope="col" className={HEAD}>
              상태
            </th>
            <th scope="col" className={HEAD}>
              FPS
            </th>
            <th scope="col" className={HEAD}>
              시각 차이
            </th>
            <th scope="col" className={HEAD}>
              수집 간격 최대
            </th>
            <th scope="col" className={HEAD}>
              전송 손실
            </th>
            <th scope="col" className={HEAD}>
              정책 건너뜀
            </th>
            <th scope="col" className={HEAD}>
              큐 대기 p95
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <th scope="row" className={cn(ROW_HEAD, 'font-mono')}>
                {row.id}
              </th>
              {/* 상태 이상은 지연이 아니라 주황을 쓰지 않고 굵게만 보인다. */}
              <td className={cn(CELL, !row.ok && 'font-semibold')}>
                <Value value={row.status} mono={false} />
              </td>
              <td className={CELL}>
                <Value value={row.fps} />
              </td>
              <td className={CELL}>
                <Value value={row.syncOffset} />
              </td>
              <td className={CELL}>
                <Value value={row.gapMax} delay />
              </td>
              <td className={CELL}>
                <Value value={row.runpodLoss} />
              </td>
              <td className={CELL}>
                <Value value={row.skipped} />
              </td>
              <td className={CELL}>
                <Value value={row.queueWaitP95} delay />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function Latency({ alive }: { alive: boolean }) {
  const health = useCurrentHealth(alive);
  const metrics = useMetrics();
  return (
    <Section title="구간별 지연">
      <table aria-label="구간별 지연" className="w-full text-sm">
        <thead>
          <tr>
            <th scope="col" className={HEAD}>
              구간
            </th>
            <th scope="col" className={HEAD}>
              p50
            </th>
            <th scope="col" className={HEAD}>
              p95
            </th>
          </tr>
        </thead>
        <tbody>
          {latencyRows(health, alive ? metrics : undefined).map((row) => (
            <tr key={row.key}>
              <th scope="row" className={ROW_HEAD}>
                {row.label}
              </th>
              <td className={CELL}>
                <Value value={row.p50} delay />
              </td>
              <td className={CELL}>
                <Value value={row.p95} delay />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function Processing({ alive }: { alive: boolean }) {
  return (
    <Section title="처리 상태">
      <Rows rows={processingRows(useCurrentHealth(alive))} className="gap-y-1.5 text-sm" />
    </Section>
  );
}

export function SystemPage() {
  const alive = useLinkAlive();
  return (
    <>
      <PageTitle>시스템 상태</PageTitle>
      <Overview alive={alive} />
      <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,1fr)] items-start gap-3">
        <Cameras alive={alive} />
        <div className="flex flex-col gap-3">
          <Latency alive={alive} />
          <Processing alive={alive} />
        </div>
      </div>
    </>
  );
}
