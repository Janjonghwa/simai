import type { HealthChipState } from '@/features/layout/status';
import { Badge } from '@simai/ui/components/badge';
import { cn } from '@simai/ui/lib/utils';

const LOOK: Record<HealthChipState, { dot: string; ring: string; label: string; text?: string }> = {
  NOMINAL: { dot: 'bg-ok', ring: 'border-ok/45', label: 'NOMINAL', text: '정상' },
  DEGRADED: {
    dot: 'bg-delay',
    ring: 'border-delay text-delay-soft',
    label: 'DEGRADED',
    text: '영상 지연·누락',
  },
  UNSAFE_UNKNOWN: {
    dot: 'bg-faint',
    ring: 'border-faint',
    label: 'UNSAFE_UNKNOWN',
    text: '판정 불가',
  },
  // 지금 상태를 모른다. 마지막으로 받은 값을 정상처럼 보이지 않는다.
  LOST: { dot: 'bg-faint', ring: 'border-faint', label: '연결 끊김' },
};

export function SystemHealthChip({ state }: { state?: HealthChipState }) {
  const look = state ? LOOK[state] : undefined;
  return (
    <Badge
      role="status"
      variant="outline"
      className={cn(
        'h-auto gap-2 rounded-full bg-surface-2 px-3.5 py-1.5 text-sm font-bold',
        look?.ring ?? 'border-border',
      )}
    >
      <i aria-hidden className={cn('size-2.5 rounded-full', look?.dot ?? 'bg-faint')} />
      {look?.label ?? '—'}
      {look?.text && <span className="font-medium opacity-80">· {look.text}</span>}
    </Badge>
  );
}
