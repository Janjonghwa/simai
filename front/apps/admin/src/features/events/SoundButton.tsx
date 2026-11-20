import { useCanAck } from '@/features/events/ack';
import { useAlarmSoundState } from '@/features/events/useAlarmSound';
import { alarmSound } from '@/lib/alarmSound';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { Volume2, VolumeX } from 'lucide-react';

/** 소리 상태와 누를 때의 동작을 알린다. */
export function soundLabel(muted: boolean, blocked: boolean) {
  if (muted) return '경보 소리 꺼짐 · 눌러서 켜기';
  return blocked ? '경보 소리 차단됨 · 눌러서 풀기' : '경보 소리 켜짐';
}

/** 확인 권한이 있는 계정의 상단 바 경보 소리 버튼. */
export function SoundButton() {
  const canAck = useCanAck();
  const { muted, ready } = useAlarmSoundState();
  if (!canAck) return null;

  const blocked = !ready;
  const label = soundLabel(muted, blocked);
  const Icon = muted || blocked ? VolumeX : Volume2;

  return (
    <Button
      variant="secondary"
      size="icon"
      aria-pressed={!muted}
      aria-label={label}
      onClick={() => alarmSound.toggle()}
      className={cn(
        'group relative overflow-visible',
        muted &&
          'border-muted-foreground bg-transparent text-muted-foreground hover:bg-surface-3 hover:text-foreground',
      )}
    >
      <Icon aria-hidden className="size-[18px]" />
      <span
        role="tooltip"
        className="pointer-events-none absolute top-full right-0 z-50 mt-2 hidden w-max rounded-md border border-border bg-card px-2 py-1 text-xs text-foreground shadow-float group-hover:block group-focus-visible:block"
      >
        {label}
      </span>
    </Button>
  );
}
