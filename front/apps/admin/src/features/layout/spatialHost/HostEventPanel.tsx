import { EventPanel } from '@/features/events/EventPanel';
import type { Incident } from '@/features/events/events';
import { useEventsStore } from '@/features/events/events.store';
import { useLiveEvents } from '@/features/events/useLiveEvents';

/**
 * 오른쪽 이벤트 패널. 이벤트 목록은 자주 바뀌어 여기서 직접 받는다(틀 전체가 다시 그려지지 않는다).
 * 패널이 없는 화면에서도 목록 조회는 이어지게 이 컴포넌트는 늘 두고 그리기만 거른다.
 */
export function HostEventPanel({
  visible,
  selectedKey,
  onSelect,
}: {
  visible: boolean;
  selectedKey: string | null;
  onSelect: (incident: Incident) => void;
}) {
  const { entries, loaded, failed } = useLiveEvents();
  const openEventId = useEventsStore((s) => s.openEventId);
  const openEvent = useEventsStore((s) => s.openEvent);
  if (!visible) return null;
  return (
    <EventPanel
      entries={entries}
      loaded={loaded}
      failed={failed}
      selectedKey={selectedKey}
      onSelect={onSelect}
      openEventId={openEventId}
      onOpen={openEvent}
    />
  );
}
