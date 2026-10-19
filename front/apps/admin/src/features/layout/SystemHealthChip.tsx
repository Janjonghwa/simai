import type { SystemHealth } from '@/features/layout/status';
import { Badge } from '@simai/ui/components/badge';
import { cn } from '@simai/ui/lib/utils';

const LOOK: Record<SystemHealth, { dot: string; ring: string; text: string }> = {
  NOMINAL: { dot: 'bg-ok', ring: 'border-ok/45', text: '정상' },
  DEGRADED: { dot: 'bg-delay', ring: 'border-delay text-delay-soft', text: '영상 지연·누락' },
  UNSAFE_UNKNOWN: { dot: 'bg-faint', ring: 'border-faint', text: '판정 불가' },
};

export function SystemHealthChip({ health }: { health?: SystemHealth }) {
  const look = health ? LOOK[health] : undefined;
  return (
    <Badge
      variant="outline"
      className={cn(
        'h-auto gap-2 rounded-full bg-surface-2 px-3.5 py-1.5 text-sm font-bold',
        look?.ring ?? 'border-border',
      )}
    >
      <i className={cn('size-2.5 rounded-full', look?.dot ?? 'bg-faint')} />
      {health ?? '—'}
      {look && <span className="font-medium opacity-80">· {look.text}</span>}
    </Badge>
  );
}
