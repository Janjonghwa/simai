import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { AckButton } from '@/features/events/AckButton';
import { EventDetailHeader } from '@/features/events/EventDetailHeader';
import { ClipsPanel, PhotosPanel } from '@/features/events/EventDetailPanels';
import type { Incident } from '@/features/events/incidents';
import { topEntry } from '@/features/events/incidents';
import { useZoneName } from '@/features/zones/useZoneName';
import { useEffect, useRef } from 'react';

interface EventDetailProps {
  incident: Incident;
  /** 눌러서 연 규칙. 근거 목록에서 표시한다. */
  openEventId: string;
  onClose: () => void;
  /** 근거 시각을 눌렀을 때 그 시각으로 이동하는 콜백. 없으면 표시만 한다. */
  onSeekTimestamp?: (timestampMs: number) => void;
}

/**
 * 사건 상세 창(관제 데모와 같다). 한 대상에 걸린 규칙 전부를 장면 위에 띄워 보인다. 바깥을 누르거나 Esc 로 닫는다.
 * 상세 API(GET /api/events/{id}) 형식이 정해지기 전이라 받은 이벤트 값으로 그린다. 조치 요청·조치 단계는 서버 API(M2)가 없어 두지 않는다.
 */
export function EventDetail({ incident, onClose }: EventDetailProps) {
  const close = useRef<HTMLButtonElement>(null);
  const { entries } = incident;
  const entry = topEntry(incident);
  const role = useCurrentRole();
  const zoneName = useZoneName();

  // 열 때(다른 사건으로 바꿀 때)만 닫기 버튼에 포커스를 둔다. 패널이 다시 그려질 때마다 옮기면 ACK 등에 둔 포커스를 빼앗는다.
  useEffect(() => {
    close.current?.focus();
  }, [incident.key]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      data-testid="incident-layer"
      onClick={onClose}
      className="absolute inset-x-0 top-[calc(60px+var(--notices-h,0px))] bottom-[86px] z-[6] flex items-start justify-end bg-foreground/14 p-4 dark:bg-background/45"
    >
      <section
        role="dialog"
        aria-label="사건 상세"
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-full min-w-0 flex-[0_1_720px] flex-col gap-3 overflow-auto rounded-[16px] bg-background/97 px-4 py-3.5 shadow-float ring-1 ring-border ring-inset"
      >
        <EventDetailHeader incident={incident} closeRef={close} onClose={onClose} />
        {entry.event.rationale && (
          <section aria-label="AI 설명">
            <h3 className="text-xs font-semibold text-muted-foreground">AI 설명</h3>
            <p className="text-sm">{entry.event.rationale}</p>
          </section>
        )}
        {role === 'ADMIN' && <AckButton entry={entry} zoneName={zoneName} />}
        <details className="min-h-0 overflow-auto">
          <summary className="cursor-pointer font-semibold">이벤트 클립</summary>
          <div className="mt-3 flex flex-col gap-3">
            <ClipsPanel entries={entries} />
            <PhotosPanel entries={entries} />
          </div>
        </details>
      </section>
    </div>
  );
}
