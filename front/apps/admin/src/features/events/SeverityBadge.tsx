import { SEVERITY_LABEL } from '@/features/events/constants';
import type { Severity } from '@/features/events/types';
import { Badge } from '@simai/ui/components/badge';
import { cn } from '@simai/ui/lib/utils';

// 위험은 빨강 ▲, 주의는 노랑 ! 로 표시한다. 기호는 화면 낭독기에서 제외한다.
const LOOK: Record<Severity, { glyph: string; className: string }> = {
  CRITICAL: { glyph: '▲', className: 'border-sev-critical bg-sev-critical text-background' },
  HIGH: { glyph: '!', className: 'border-sev-caution text-sev-caution' },
  CAUTION: { glyph: '!', className: 'border-sev-caution text-sev-caution' },
  INFO: { glyph: 'i', className: 'border-faint text-muted-foreground' },
};

export function SeverityBadge({
  severity,
  className,
  size = 'small',
}: {
  severity: Severity;
  className?: string;
  size?: 'small' | 'large';
}) {
  const look = LOOK[severity];
  return (
    // 기호 폭(! 는 ▲ 보다 좁다)과 글자에 상관없이 등급 배지 크기를 같게 둔다: 기호 칸 1em 고정 + 최소 폭.
    <Badge
      variant="outline"
      className={cn(
        'justify-center rounded-[4px] font-bold',
        size === 'large' ? 'h-7 min-w-[5rem] text-sm' : 'min-w-[4.25rem]',
        look.className,
        className,
      )}
    >
      <span aria-hidden className="inline-block w-[1em] text-center">
        {look.glyph}
      </span>
      {SEVERITY_LABEL[severity]}
    </Badge>
  );
}
