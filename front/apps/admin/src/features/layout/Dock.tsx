import { buttonVariants } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { Link } from '@tanstack/react-router';
import { FileText, History, LayoutGrid, Monitor, Video } from 'lucide-react';

const ITEMS = [
  { to: '/', label: '실시간 관제', short: '실시간 관제', icon: Monitor },
  { to: '/cctv', label: 'CCTV 관제', short: 'CCTV 관제', icon: Video },
  { to: '/zones', label: '구역 편집', short: '구역 편집', icon: LayoutGrid },
  { to: '/tbm', label: '레이아웃 · TBM 보고서', short: 'TBM 보고서', icon: FileText },
  { to: '/history', label: '히스토리', short: '히스토리', icon: History },
] as const;

// 평소에는 흐리게 두어 장면을 덜 가리고, 마우스를 올리거나 키보드 초점이 들어오면 진하게 보인다(관제 데모와 같다).
export function Dock() {
  return (
    <nav
      aria-label="주 메뉴"
      className="absolute bottom-3.5 left-1/2 z-[6] flex -translate-x-1/2 gap-0.5 rounded-[16px] bg-card/94 p-1.5 opacity-30 shadow-float ring-1 ring-border backdrop-blur-[8px] transition-opacity duration-200 ring-inset hover:opacity-100 has-focus-visible:opacity-100"
    >
      {ITEMS.map(({ to, label, short, icon: Icon }) => (
        <Link
          key={to}
          to={to}
          aria-label={label}
          activeOptions={{ exact: to === '/' }}
          className={cn(
            buttonVariants({ variant: 'ghost' }),
            'h-auto min-h-[58px] min-w-[92px] flex-col gap-1 rounded-[12px] px-2.5 py-1.5 text-xs font-normal text-muted-foreground',
            // ghost 의 dark:hover 색보다 앞서도록 dark: 를 붙이고, 현재 항목 배경은 hover 중에도 유지한다.
            'hover:text-foreground dark:hover:bg-surface-2',
            'data-[status=active]:bg-surface-3! data-[status=active]:font-bold data-[status=active]:text-foreground data-[status=active]:shadow-[inset_0_-2px_0_var(--primary)]',
          )}
        >
          <Icon className="size-5 opacity-85 in-data-[status=active]:text-point-soft in-data-[status=active]:opacity-100" />
          <span>{short}</span>
        </Link>
      ))}
    </nav>
  );
}
