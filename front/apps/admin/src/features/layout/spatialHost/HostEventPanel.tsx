import { latestStageStart, withoutStaleAcked } from '@/features/events/entries';
import { useEventClearStore } from '@/features/events/eventClear.store';
import { EventPanel } from '@/features/events/EventPanel';
import type { Incident } from '@/features/events/events';
import { useEventsStore } from '@/features/events/events.store';
import { ec2Now } from '@/features/events/format';
import type { EventItem } from '@/features/events/types';
import { useMinuteNow } from '@/features/events/useSharedNow';
import { useVisibleLiveEvents } from '@/features/events/useVisibleLiveEvents';
import { PastEventsLink } from '@/features/monitor/PastEventsLink';
import { useMonitorWorks } from '@/features/monitor/useMonitorWorks';
import { isOutsideWork, splitSince, workStartedAt } from '@/features/monitor/workSession';
import { useRealtimeStore } from '@/store/realtime.store';
import { useCallback, useMemo } from 'react';

/**
 * 오른쪽 이벤트 패널. 이벤트 목록은 자주 바뀌어 여기서 직접 받는다(틀 전체가 다시 그려지지 않는다).
 * 패널이 없는 화면에서도 목록 조회는 이어지게 이 컴포넌트는 늘 두고 그리기만 거른다.
 * 「모두 지우기」는 화면에서만 지운다(서버·이벤트 캐시는 그대로).
 *
 * 진행 중인 작업이 있으면 그 작업이 시작된 뒤의 이벤트만 기본으로 보이고(확인 대기 경보는 가리지 않는다),
 * 지난 이벤트는 히스토리 링크로 간다. 경보 배너·소리·2D 평면은 이 거름과 상관없이 전체를 쓴다.
 * 작업 구역 밖이거나 진행 중인 작업이 없을 때 난 이벤트에는 「작업 외 진입」 꼬리표가 붙는다.
 * 운영자가 확인한 지 24시간(ACK_HIDE_AFTER_MS)이 지난 이벤트는 이 패널에서만 숨긴다(배너·소리·2D 평면은 그대로).
 * 작업 목록은 관제 화면(MonitorPage·세션 바)이 받아 둔 캐시만 구독한다(여기서 요청하지 않는다).
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
  const { entries: visibleEntries, loaded, failed } = useVisibleLiveEvents();
  const works = useMonitorWorks({ passive: true });
  const active = works.state === 'ready' ? works.active : null;
  const since = active ? workStartedAt(active) : null;
  const { shown: sinceShown, hidden } = useMemo(
    () => splitSince(visibleEntries, since),
    [visibleEntries, since],
  );
  // 확인한 지 오래된(24시간) 이벤트는 패널에서만 뺀다. 분이 바뀔 때만 다시 따져 목록을 매 초 다시 그리지 않는다.
  const minuteNow = useMinuteNow();
  // EC2 시계 차이(health, 매 초 갱신)는 구독하지 않고 다시 따질 때만 읽는다: 구독하면 패널이 매 초 다시 그려진다.
  const entries = useMemo(
    () =>
      withoutStaleAcked(
        sinceShown,
        ec2Now(minuteNow, useRealtimeStore.getState().health?.clockOffsetMs),
      ),
    [sinceShown, minuteNow],
  );
  const knownWorks = works.state === 'ready' ? works.works : null;
  const outsideWork = useCallback(
    (event: EventItem) => knownWorks !== null && isOutsideWork(event, knownWorks),
    [knownWorks],
  );
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
      isOutsideWork={knownWorks !== null ? outsideWork : undefined}
      footer={since !== null ? <PastEventsLink hidden={hidden} /> : undefined}
    />
  );
}
