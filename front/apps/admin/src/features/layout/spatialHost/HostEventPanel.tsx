import { latestStageStart } from '@/features/events/entries';
import { useEventClearStore } from '@/features/events/eventClear.store';
import { EventPanel } from '@/features/events/EventPanel';
import type { Incident } from '@/features/events/events';
import { useEventsStore } from '@/features/events/events.store';
import { useVisibleLiveEvents } from '@/features/events/useVisibleLiveEvents';

/**
 * 오른쪽 이벤트 패널. 이벤트 목록은 자주 바뀌어 여기서 직접 받는다(틀 전체가 다시 그려지지 않는다).
 * 패널이 없는 화면에서도 목록 조회는 이어지게 이 컴포넌트는 늘 두고 그리기만 거른다.
 * 「모두 지우기」는 화면에서만 지운다(서버·이벤트 캐시는 그대로).
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
  const { entries, loaded, failed } = useVisibleLiveEvents();
  const openEventId = useEventsStore((s) => s.openEventId);
  const openEvent = useEventsStore((s) => s.openEvent);
  const clearAll = useEventClearStore((s) => s.clearAll);
  const clearShown = () => {
    const at = latestStageStart(entries);
    if (at !== null) clearAll(at);
  };
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
      onClearAll={clearShown}
    />
  );
}
