import { EventPhotos } from '@/features/eventPhotos/EventPhotos';
import { isActive, isConfirmationPending } from '@/features/events/entries';
import { OUTSIDE_WORK_TEXT, type OutsideWorkKind } from '@/features/events/EventBadges';
import { confirmationText, formatTime } from '@/features/events/format';
import { firstActiveRaisedAt, topEntry, type Incident } from '@/features/events/incidents';
import { cardChip, cardTitle, targetLabel } from '@/features/events/incidentView';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import type { Severity } from '@/features/events/types';
import { usePersonDisplayMap } from '@/features/people/usePersonDisplay';
import { useZoneName } from '@/features/zones/useZoneName';
import { cn } from '@simai/ui/lib/utils';
import { useEffect, useRef } from 'react';

/** 발생 횟수 글(올려 보면 나오는 설명 한 줄). 트랙 번호가 바뀌며 이어진 경보를 묶었으면(S15P31A101-731) 번호 수를 붙인다. */
const occurrencesText = (occurrences: number, people: number) =>
  people > 1
    ? `${occurrences}회 발생(트랙 번호 ${people}개를 한 카드로 묶음)`
    : `${occurrences}회 발생`;

interface IncidentCardProps {
  incident: Incident;
  /** 카드 대표 위험(규칙·구역)의 발생 횟수(S15P31A101-731 화면 묶음 포함). 2 이상이면 올려 보면 나오는 설명에 적는다. */
  occurrences?: number;
  /** 카드에 묶인 대상(사람 트랙 번호) 수. 2 이상이면 올려 보면 나오는 설명에 묶은 번호 수를 적는다. */
  people?: number;
  selected: boolean;
  /**
   * 대상 사람이 지금 카메라에서 사라졌다(S15P31A101-757). 카드를 흐리게 하고 점멸하지 않는다(「최근 해소」 칸으로 옮긴 카드,
   * S15P31A101-798). 경보 배너·소리는 이 값과 상관없다.
   */
  gone?: boolean;
  /** 작업 외 진입의 이유(작업 시간 외·작업 구역 밖). 올려 보면 나오는 설명에 적는다. 등급·소리·표시는 그대로다. */
  outsideWork?: OutsideWorkKind | null;
  onSelect: (incident: Incident) => void;
  onOpen: (eventId: string) => void;
}

/** 등급 색: 위험 빨강, 경고 주황, 주의 노랑, 안내 회색. 띠는 늘 있고, 테두리는 진행 중인 묶음에만 있다. */
const SEVERITY_LOOK: Record<Severity, { bar: string; ring?: string }> = {
  CRITICAL: { bar: 'bg-sev-critical', ring: 'ring-1 ring-sev-critical ring-inset' },
  HIGH: { bar: 'bg-sev-high', ring: 'ring-1 ring-sev-high ring-inset' },
  CAUTION: { bar: 'bg-sev-caution', ring: 'ring-1 ring-sev-caution ring-inset' },
  INFO: { bar: 'bg-faint' },
};

/**
 * 한 사람(또는 구역)에 걸린 규칙 묶음 카드(명세 7.4, 관제 데모의 IncCard). 한 줄이다(S15P31A101-798): 등급 배지 · 대표 규칙(가장 높은
 * 등급) 이름뿐이다. 처리 상태(미확인 · 확인 시각 · 해소 시각 · 지난 관제, cardChip)·대상(작업자·구역)·발생 시각·발생 횟수·확인 필요·
 * 지연 알림·작업 외 꼬리표는 줄에서 빼고 머리를 올려 보면 나오는 설명에 둔다(대상은 카드 이름에도 있다). 칩만 뺐고, 미확인 경보의
 * 점멸·등급 테두리·경보 배너·소리는 그대로다. 진입 예상 줄과 「CCTV로 확인」은 없다. 사람을 고르면 떠 있는 CCTV 창이 그 사람의 카메라로 열린다.
 * 지난 관제(지금 Tier 0 가 들고 있지 않음, S15P31A101-595)와 사라진 사람의 카드는 등급 테두리·점멸 없이 흐리게 보인다.
 * 나머지 규칙 · 개구부 수치 · 처리는 사건 상세에서 본다. 머리는 처음 누르면 선택(3D 초점), 이미 선택된 카드를 다시 누르면 사건 상세를 연다.
 */
export function IncidentCard({
  incident,
  occurrences = 1,
  people = 1,
  selected,
  gone = false,
  outsideWork = null,
  onSelect,
  onOpen,
}: IncidentCardProps) {
  const zoneName = useZoneName();
  const personNames = usePersonDisplayMap();
  const top = topEntry(incident);
  const look = SEVERITY_LOOK[incident.severity];
  const chip = cardChip(incident);
  const live = incident.entries.filter((e) => isActive(e.event));
  const check = live.find(isConfirmationPending);
  const delayed = live.some((e) => e.event.delivery === 'DELAYED_ACTIVE');
  const raisedAt = incident.active ? firstActiveRaisedAt(incident) : null;
  // 지금 관제 중인 사건만 등급 테두리·점멸을 쓴다. 지난 관제·사라진 사람은 지금 카메라에 없다.
  const current = incident.active && !incident.stale && !gone;
  const fullTarget = targetLabel(incident.target, zoneName, personNames);
  const detail = [
    fullTarget,
    chip?.label ?? null,
    raisedAt !== null ? `발생 ${formatTime(raisedAt)}` : null,
    occurrences > 1 ? occurrencesText(occurrences, people) : null,
    check ? confirmationText(check.event) : null,
    delayed ? '지연 알림' : null,
    outsideWork ? OUTSIDE_WORK_TEXT[outsideWork].label : null,
  ]
    .filter((line): line is string => line !== null)
    .join('\n');

  // 3D·2D 에서 사람을 눌러 이 묶음이 선택되면 긴 목록에서도 보이게 한다.
  const card = useRef<HTMLElement>(null);
  useEffect(() => {
    if (selected) card.current?.scrollIntoView?.({ block: 'nearest' });
  }, [selected]);

  return (
    <li>
      <article
        ref={card}
        aria-label={`${fullTarget} 이벤트`}
        className={cn(
          'relative flex flex-col gap-1 overflow-hidden rounded-[10px] bg-surface-2 py-2 pr-3 pl-[18px]',
          'hover:ring-1 hover:ring-point-soft hover:ring-inset',
          current && look.ring,
          !incident.active && 'opacity-55',
          incident.stale && 'opacity-75',
          gone && 'opacity-60',
          selected && 'outline-2 outline-offset-1 outline-primary',
        )}
      >
        <span
          aria-hidden
          className={cn(
            'absolute inset-y-0 left-0 w-1',
            look.bar,
            incident.alarming && current && 'motion-safe:animate-alarm-blink',
          )}
        />
        <button
          type="button"
          aria-pressed={selected}
          title={detail}
          onClick={() => (selected ? onOpen(top.event.event_id) : onSelect(incident))}
          className="-mx-1 flex min-w-0 items-center gap-2 rounded-md px-1 py-0.5 text-left hover:bg-surface-3"
        >
          <SeverityBadge severity={top.event.severity} />
          <b className="min-w-0 flex-1 truncate">{cardTitle(incident)}</b>
        </button>
        {selected && <EventPhotos event={top.event} compact />}
      </article>
    </li>
  );
}
