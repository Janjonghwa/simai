import { buttonVariants } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { Link } from '@tanstack/react-router';
import { Activity, FileText, LayoutGrid, Monitor, Video } from 'lucide-react';

const ITEMS = [
  { to: '/', label: '실시간 관제', short: '실시간 관제', icon: Monitor },
  { to: '/cctv', label: 'CCTV 관제', short: 'CCTV 관제', icon: Video },
  { to: '/zones', label: '구역 편집', short: '구역 편집', icon: LayoutGrid },
  { to: '/tbm', label: '레이아웃 · TBM 보고서', short: 'TBM 보고서', icon: FileText },
  { to: '/system', label: '시스템 상태', short: '시스템 상태', icon: Activity },
] as const;

export function Dock() {
  return (
    <nav
      aria-label="주 메뉴"
      className="absolute bottom-3.5 left-1/2 z-[6] flex -translate-x-1/2 gap-0.5 rounded-[16px] bg-card/94 p-1.5 shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset"
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
