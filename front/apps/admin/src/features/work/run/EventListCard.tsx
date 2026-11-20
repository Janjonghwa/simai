import { IncidentCard } from '@/features/events/IncidentCard';
import { groupIncidents } from '@/features/events/incidents';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { useNavigate } from '@tanstack/react-router';
import { useMemo, useState } from 'react';

// 작업 종료 단계 오른쪽 가운데 카드: 지금 실행을 시작한 뒤 난 경보 기록(위험·경고)을 최근 것부터(S15P31A101-731).
// 머리줄 수는 경보 기록 한 건 한 건(작업 목록·달력·종료 미팅의 위험·경고 수와 같은 정의)이고, 관제 화면의 「진행 중 n건」(지금 진행 중인
// 사건 카드)과 이름을 가른다. 줄은 종류(규칙)·구역을 보이고, 같은 초·같은 대상의 위험·경고 쌍은 한 줄이다. 구역을 모르면 '현장'.

const COUNT_TITLE = '이번 실행의 위험·주의 경보 기록 수입니다.';

/**
 * 경보 기록. startedAt 은 지금 실행을 시작한 시각(EC2 ms)이고, 없으면 기록이 없는 것으로 본다. zoneId 는 구역 작업의 구역이다
 * (종료 미팅처럼 그 구역 경보만 센다, 없으면 현장 전체).
 */
export function EventListCard({
  startedAt,
  zoneId = null,
}: {
  startedAt: number | null;
  zoneId?: string | null;
}) {
  const { entries } = useLiveEvents();
  const navigate = useNavigate();
  const [selected, setSelected] = useState<string | null>(null);
  const filtered = useMemo(
    () =>
      entries.filter(
        ({ event }) =>
          startedAt !== null &&
          event.source === 'LIVE' &&
          event.severity !== 'INFO' &&
          event.raised_at !== undefined &&
          event.raised_at >= startedAt &&
          (zoneId === null || event.zone_id === zoneId),
      ),
    [entries, startedAt, zoneId],
  );
  const rows = useMemo(
    () =>
      groupIncidents(filtered).sort(
        (a, b) =>
          Math.max(...b.entries.map(({ event }) => event.raised_at ?? 0)) -
          Math.max(...a.entries.map(({ event }) => event.raised_at ?? 0)),
      ),
    [filtered],
  );
  const critical = filtered.filter(({ event }) => event.severity === 'CRITICAL').length;
  return (
    <Card size="sm" className="flex min-h-0 flex-1 flex-col">
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2">
          <h2>경보 기록</h2>
          <span
            title={COUNT_TITLE}
            className="text-sm font-normal text-muted-foreground tabular-nums"
          >
            {filtered.length}건
            {filtered.length > 0 && ` · 위험 ${critical} · 주의 ${filtered.length - critical}`}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="min-h-0 flex-1 overflow-auto">
        {rows.length === 0 ? (
          <p className="text-muted-foreground">경보 없음</p>
        ) : (
          <ul aria-label="경보 기록 목록" className="flex flex-col gap-2">
            {rows.map((row) => (
              <IncidentCard
                key={row.key}
                incident={row}
                selected={selected === row.key}
                onSelect={(incident) => setSelected(incident.key)}
                onOpen={() => void navigate({ to: '/live' })}
              />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
