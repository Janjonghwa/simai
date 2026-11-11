import { CameraButton } from '@/features/events/CameraButton';
import { isActive, needsConfirmation } from '@/features/events/entries';
import {
  ConfirmBadge,
  DelayedBadge,
  OutsideWorkBadge,
  StateChip,
} from '@/features/events/EventBadges';
import { entryEtaLine } from '@/features/events/format';
import { firstActiveRaisedAt, topEntry, type Incident } from '@/features/events/incidents';
import { cardChip, cardTitle, targetFont, targetLabel } from '@/features/events/incidentView';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { Since } from '@/features/events/Since';
import type { Severity } from '@/features/events/types';
import { useZoneName } from '@/features/zones/useZoneName';
import { cn } from '@simai/ui/lib/utils';
import { useEffect, useRef } from 'react';

interface IncidentCardProps {
  incident: Incident;
  /** 최근 해소 칸 카드에서만: 같은 대상·규칙·구역에서 발생한 횟수. 2 이상이면 「N회 발생」을 보인다. */
  occurrences?: number;
  selected: boolean;
  /** 작업 외 진입 꼬리표를 붙일지(작업 구역 밖이거나 진행 중인 작업이 없을 때 난 이벤트). 등급·소리·표시는 그대로다. */
  outsideWork?: boolean;
  onSelect: (incident: Incident) => void;
  onOpen: (eventId: string) => void;
}

/**
 * 등급 색: 위험 빨강, 경고 주황, 주의 노랑, 안내 회색. 띠는 늘 있고, 테두리는 진행 중인 묶음에만 있다.
 * 작업자 칩은 진행 중이면 채우고(chip) 해소되면 테두리만 남긴다(chipDone).
 */
const SEVERITY_LOOK: Record<
  Severity,
  { bar: string; ring?: string; chip: string; chipDone: string }
> = {
  CRITICAL: {
    bar: 'bg-sev-critical',
    ring: 'ring-1 ring-sev-critical ring-inset',
    chip: 'bg-sev-critical text-background',
    chipDone: 'border-sev-critical text-sev-critical',
  },
  HIGH: {
    bar: 'bg-sev-high',
    ring: 'ring-1 ring-sev-high ring-inset',
    chip: 'bg-sev-high text-background',
    chipDone: 'border-sev-high text-sev-high',
  },
  CAUTION: {
    bar: 'bg-sev-caution',
    ring: 'ring-1 ring-sev-caution ring-inset',
    chip: 'bg-sev-caution text-background',
    chipDone: 'border-sev-caution text-sev-caution',
  },
  INFO: {
    bar: 'bg-faint',
    chip: 'bg-faint text-background',
    chipDone: 'border-faint text-muted-foreground',
  },
};

/**
 * 한 사람(또는 구역)에 걸린 규칙 묶음 카드(명세 7.4, 관제 데모의 IncCard).
 * - 첫 줄: 작업자 칩(해소 카드는 「N회 발생」도), 오른쪽에 처리 상태 칩 하나.
 * - 둘째 줄: 등급 배지 · 대표 규칙(가장 높은 등급) 이름 · 구역.
 * - 확인 필요 · 지연 알림 꼬리표(심각도와 함께 보여야 하는 것만, 명세 7.3). 셋째 줄은 진입 예측일 때만.
 * - 진행 중이면 경과(N초 전)와 카메라 버튼.
 * - 지난 관제(지금 Tier 0 가 들고 있지 않음, S15P31A101-595)는 등급 테두리·점멸·카메라 버튼 없이 흐리게 보인다(경과는 보인다).
 * 나머지 규칙 · 개구부 수치 · 처리는 사건 상세에서 본다. 머리는 처음 누르면 선택(3D 초점),
 * 이미 선택된 카드를 다시 누르면 사건 상세를 연다.
 */
export function IncidentCard({
  incident,
  occurrences = 1,
  selected,
  outsideWork = false,
  onSelect,
  onOpen,
}: IncidentCardProps) {
  const zoneName = useZoneName();
  const top = topEntry(incident);
  const look = SEVERITY_LOOK[incident.severity];
  const chip = cardChip(incident);
  const live = incident.entries.filter((e) => isActive(e.event));
  const check = live.find((e) => needsConfirmation(e.event));
  const delayed = live.some((e) => e.event.delivery === 'DELAYED_ACTIVE');
  const raisedAt = incident.active ? firstActiveRaisedAt(incident) : null;
  // 지금 관제 중인 사건만 등급 테두리·카메라 버튼을 쓴다. 지난 관제의 사람은 지금 카메라에 없다.
  const current = incident.active && !incident.stale;
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
        aria-label={`${targetLabel(incident.target, zoneName)} 이벤트`}
        className={cn(
          'relative flex flex-col gap-1 overflow-hidden rounded-[10px] bg-surface-2 py-3 pr-3.5 pl-[18px]',
          'hover:ring-1 hover:ring-point-soft hover:ring-inset',
          current && look.ring,
          !incident.active && 'opacity-55',
          incident.stale && 'opacity-75',
          selected && 'outline-2 outline-offset-1 outline-primary',
        )}
      >
        <span
          aria-hidden
          className={cn(
            'absolute inset-y-0 left-0 w-1',
            look.bar,
            incident.alarming && 'motion-safe:animate-alarm-blink',
          )}
        />
        <button
          type="button"
          aria-pressed={selected}
          onClick={() => (selected ? onOpen(top.event.event_id) : onSelect(incident))}
          className="-mx-1 flex flex-col gap-1.5 rounded-md px-1 py-0.5 text-left hover:bg-surface-3"
        >
          <span className="flex items-center justify-between gap-2">
            <span className="flex min-w-0 items-center gap-2">
              <span
                className={cn(
                  'rounded-md border-2 border-transparent px-2 py-px text-sm font-bold',
                  targetFont(incident.target),
                  current ? look.chip : look.chipDone,
                )}
              >
                {targetLabel(incident.target, zoneName)}
              </span>
              {occurrences > 1 && (
                <span className="text-sm whitespace-nowrap text-muted-foreground">
                  {occurrences}회 발생
                </span>
              )}
            </span>
            {chip && <StateChip chip={chip} severity={incident.severity} />}
          </span>
          <span className="flex min-w-0 items-center gap-2">
            <SeverityBadge severity={top.event.severity} />
            <b className="truncate">{cardTitle(incident, zoneName)}</b>
          </span>
        </button>
        {(check || delayed || outsideWork) && (
          <div className="flex flex-wrap gap-1.5">
            {check && <ConfirmBadge event={check.event} compact />}
            {delayed && <DelayedBadge compact />}
            {outsideWork && <OutsideWorkBadge />}
          </div>
        )}
        {eta && <p className="text-sm">{eta}</p>}
        {raisedAt !== null && (
          <Since raisedAt={raisedAt} className="self-end text-xs text-muted-foreground" />
        )}
        <CameraButton target={current ? incident.target : null} variant="card" />
      </article>
    </li>
  );
}
