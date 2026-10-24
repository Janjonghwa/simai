import { dataAgeSec, FAILURE_TITLE, failureOf, lastDataClock } from '@/features/layout/status';
import { useNow } from '@/features/layout/useNow';
import { useLinkAlive } from '@/hooks/useLinkAlive';
import { useRealtimeStore } from '@/store/realtime.store';

/** 마지막으로 데이터를 받은 시각과 그 뒤 흐른 시간. 1초마다 바뀐다. */
function LastReceived() {
  const now = useNow();
  const age = useRealtimeStore((s) => dataAgeSec(s.health, now));
  const at = useRealtimeStore((s) => lastDataClock(s.health));
  if (age === undefined || at === undefined) return null;
  return (
    <span className="text-sm whitespace-nowrap">
      마지막 수신 <span className="font-mono">{at}</span> · {Math.floor(age)}초 경과
    </span>
  );
}

/**
 * 장애 띠(명세 FR-11). Tier 0 정지·노트북 정지·Tier 0 → EC2 단절을 EC2 가 알리면 모든 화면 상단 바 아래에 띄운다.
 * EC2 와 끊겼을 때는 상단 바의 LOST 만 보인다(지금 상태를 모른다).
 */
export function FailureBand() {
  const alive = useLinkAlive();
  const failure = useRealtimeStore((s) => failureOf(s.health, alive));
  if (!failure) return null;
  return (
    <div
      role="group"
      aria-label="장애"
      className="pointer-events-auto flex max-w-[680px] flex-wrap items-baseline gap-x-4 gap-y-1 rounded-[12px] bg-stripes px-4 py-2.5 shadow-float"
    >
      {/* 장애 종류가 바뀔 때만 읽는다. 경과 시간은 읽지 않는다. */}
      <strong role="alert" className="text-base font-bold">
        {FAILURE_TITLE[failure]}
      </strong>
      <LastReceived />
    </div>
  );
}
