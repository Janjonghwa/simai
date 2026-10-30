import { CameraButton } from '@/features/events/CameraButton';
import { isActive, isAlarmSeverity, needsConfirmation } from '@/features/events/entries';
import { ConfirmBadge, DelayedBadge, StateChip } from '@/features/events/EventBadges';
import { entryEtaLine, ruleName } from '@/features/events/format';
import { firstActiveRaisedAt, topEntry, type Incident } from '@/features/events/incidents';
import { cardChip, placeLine, targetLabel } from '@/features/events/incidentView';
import type { Mismatches } from '@/features/events/mismatch';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { Since } from '@/features/events/Since';
import type { Severity } from '@/features/events/types';
import { cn } from '@simai/ui/lib/utils';
import { useEffect, useRef } from 'react';

interface IncidentCardProps {
  incident: Incident;
  selected: boolean;
  zoneName?: (zoneId: string) => string | undefined;
  /** 조치 불일치(관리자 전용 /topic/admin/events). */
  mismatches?: Mismatches;
  onSelect: (incident: Incident) => void;
  onOpen: (eventId: string) => void;
}

type Tier = 'alarm' | 'caution' | 'info';

const tierOf = (severity: Severity): Tier => {
  if (isAlarmSeverity(severity)) return 'alarm';
  if (severity === 'CAUTION') return 'caution';
  return 'info';
};

/** 등급 색: 경고 이상은 빨강, 주의는 노랑, 안내는 회색. 띠는 늘 있고, 테두리는 진행 중인 묶음에만 있다. */
const TIER_CLASS: Record<Tier, { bar: string; ring?: string }> = {
  alarm: { bar: 'bg-sev-critical', ring: 'ring-1 ring-sev-critical ring-inset' },
  caution: { bar: 'bg-sev-caution', ring: 'ring-1 ring-sev-caution ring-inset' },
  info: { bar: 'bg-faint' },
};

/**
 * 한 사람(또는 구역)에 걸린 규칙 묶음 카드(명세 7.4, 관제 데모의 IncCard).
 * - 머리줄: 등급 배지 · 대표 규칙(가장 높은 등급) 이름, 오른쪽에 처리 상태 칩 하나.
 * - 확인 필요 · 지연 알림 꼬리표(심각도와 함께 보여야 하는 것만, 명세 7.3).
 * - 둘째 줄: 대상 · 구역 · 경과(N초 전). 셋째 줄은 진입 예측일 때만. 카메라가 있으면 카메라 버튼.
 * 나머지 규칙 · 개구부 수치 · 처리는 사건 상세에서 본다. 머리줄은 처음 누르면 선택(3D 초점),
 * 이미 선택된 카드를 다시 누르면 사건 상세를 연다.
 */
export function IncidentCard({
  incident,
  selected,
  zoneName,
  mismatches,
  onSelect,
  onOpen,
}: IncidentCardProps) {
  const top = topEntry(incident);
  const tier = TIER_CLASS[tierOf(incident.severity)];
  const chip = cardChip(incident, { mismatches });
  const live = incident.entries.filter((e) => isActive(e.event));
  const check = live.find((e) => needsConfirmation(e.event));
  const delayed = live.some((e) => e.event.delivery === 'DELAYED_ACTIVE');
  const raisedAt = incident.active ? firstActiveRaisedAt(incident) : null;
  const eta = entryEtaLine(top.event);

  // 3D·2D 에서 사람을 눌러 이 묶음이 선택되면 긴 목록에서도 보이게 한다.
  const card = useRef<HTMLElement>(null);
  useEffect(() => {
    if (selected) card.current?.scrollIntoView?.({ block: 'nearest' });
  }, [selected]);

  return (
    <li>
      <article
        ref={card}
        aria-label={`${targetLabel(incident.target)} 이벤트`}
        className={cn(
          'relative flex flex-col gap-1 overflow-hidden rounded-[10px] bg-surface-2 py-3 pr-3.5 pl-[18px]',
          'hover:ring-1 hover:ring-point-soft hover:ring-inset',
          incident.active && tier.ring,
          !incident.active && 'opacity-55',
          selected && 'outline-2 outline-offset-1 outline-primary',
        )}
      >
        <span
          aria-hidden
          className={cn(
            'absolute inset-y-0 left-0 w-1',
            tier.bar,
            incident.alarming && 'motion-safe:animate-alarm-blink',
          )}
        />
        <button
          type="button"
          aria-pressed={selected}
          onClick={() => (selected ? onOpen(top.event.event_id) : onSelect(incident))}
          className="-mx-1 flex items-center justify-between gap-2 rounded-md px-1 text-left hover:bg-surface-3"
        >
          <span className="flex min-w-0 items-center gap-2">
            <SeverityBadge severity={top.event.severity} />
            <b className="truncate">{ruleName(top.event.rule_id)}</b>
          </span>
          {chip && <StateChip chip={chip} />}
        </button>
        {(check || delayed) && (
          <div className="flex flex-wrap gap-1.5">
            {check && <ConfirmBadge event={check.event} compact />}
            {delayed && <DelayedBadge compact />}
          </div>
        )}
        <p className="flex items-baseline justify-between gap-2 text-sm text-muted-foreground">
          <span className="min-w-0 truncate">{placeLine(incident, zoneName)}</span>
          {raisedAt !== null && <Since raisedAt={raisedAt} className="shrink-0 text-xs" />}
        </p>
        {eta && <p className="text-sm">{eta}</p>}
        <CameraButton target={incident.active ? incident.target : null} variant="card" />
      </article>
    </li>
  );
}
