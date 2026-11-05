import {
  bandFailure,
  dataAgeSec,
  FAILURE_DETAIL,
  FAILURE_TITLE,
  lastDataClock,
} from '@/features/layout/status';
import { useNow } from '@/features/layout/useNow';
import { useMonitorWorks } from '@/features/monitor/useMonitorWorks';
import { useLinkAlive } from '@/hooks/useLinkAlive';
import { useRealtimeStore } from '@/store/realtime.store';

/** 마지막으로 데이터를 받은 시각과 그 뒤 흐른 시간. 1초마다 바뀐다. */
function LastReceived() {
  const now = useNow();
  const age = useRealtimeStore((s) => dataAgeSec(s.health, now));
  const at = useRealtimeStore((s) => lastDataClock(s.health));
  if (age === undefined || at === undefined) return null;
  return (
    <span className="text-[13px] leading-[18px] text-muted-foreground">
      마지막 수신 <span className="font-mono">{at}</span> · {Math.floor(age)}초 경과 · 화면은
      마지막으로 받은 상태
    </span>
  );
}

/**
 * 장애 띠(명세 FR-11). Tier 0 정지·노트북 정지·Tier 0 → EC2 단절을 EC2 가 알리면 모든 화면 상단 바 아래에 띄운다.
 * 이 화면과 EC2 의 연결이 끊기면(지금 상태를 모른다) "현장 연결 끊김"을 띄운다.
 */
export function FailureBand() {
  const alive = useLinkAlive();
  const failure = useRealtimeStore((s) => bandFailure(s, alive));
  const works = useMonitorWorks();
  if (!failure || (failure === 'DEVICE_DOWN' && works.state === 'ready' && !works.active))
    return null;
  const detail = FAILURE_DETAIL[failure];
  return (
    <div
      role="group"
      aria-label="장애"
      className="pointer-events-auto flex max-w-[680px] flex-col gap-0.5 rounded-[12px] bg-stripes px-4 py-2.5 shadow-float ring-1 ring-border ring-inset"
    >
      {/* 장애 종류가 바뀔 때만 읽는다. 경과 시간은 읽지 않는다. */}
      <p className="text-[15px] leading-[22px] font-bold">
        <strong role="alert">{FAILURE_TITLE[failure]}</strong>
        {detail && <span> · {detail}</span>}
      </p>
      <LastReceived />
    </div>
  );
}
