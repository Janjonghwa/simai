import { ANY_CAMERA, useCamSplitStore } from '@/features/cctv/camSplit.store';
import { bestCamera, type Incident } from '@/features/events/incidents';
import { useRealtimeStore } from '@/store/realtime.store';
import { cn } from '@simai/ui/lib/utils';
import { Video } from 'lucide-react';

/** 놓이는 자리별 모양: 배너는 버튼 줄 높이에 맞추고, 카드는 본문 아래 작은 칩으로 둔다. */
const LOOK = {
  banner: 'h-8 rounded-lg bg-secondary px-2.5 text-secondary-foreground hover:bg-secondary/80',
  card: 'mt-0.5 w-fit rounded-md bg-surface-3 px-2 py-1 hover:bg-surface-3/70',
} as const;

/**
 * 카메라 버튼: 이 사람을 지금 담고 있는 카메라의 미리보기를 실시간 관제 오른쪽에 연다(관제 데모와 같다).
 * 담은 카메라가 없으면(추적이 꺼졌거나 사람을 놓침, 구역 대상) 카메라 이름 없이 「CCTV로 확인」으로 첫 카메라를 연다.
 * 대상이 없으면 버튼이 없다(해소된 묶음).
 * 카메라 이름만 고르는 셀렉터라 world 가 10Hz 로 와도 바뀔 때만 다시 그린다.
 */
export function CameraButton({
  target,
  variant,
}: {
  target: Incident['target'];
  variant: keyof typeof LOOK;
}) {
  const camId = useRealtimeStore((s) => bestCamera(s.world?.payload.tracks, target));
  const open = useCamSplitStore((s) => s.open);
  if (!target) return null;
  return (
    <button
      type="button"
      onClick={() => open({ cam: camId ?? ANY_CAMERA, target })}
      className={cn('inline-flex items-center gap-1.5 text-sm', LOOK[variant])}
    >
      <Video aria-hidden className="size-[15px]" />
      <span>
        {camId ? (
          <>
            <b className="font-mono">{camId}</b>로 확인
          </>
        ) : (
          'CCTV로 확인'
        )}
      </span>
    </button>
  );
}
