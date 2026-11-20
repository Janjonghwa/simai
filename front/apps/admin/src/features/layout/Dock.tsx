import { toDateKey } from '@/features/work/workDay';
import { useWorkNavStore } from '@/features/work/workNav.store';
import { buttonVariants } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { Link } from '@tanstack/react-router';
import { FileText, Monitor, Video, type LucideIcon } from 'lucide-react';

const ITEM_CLASS = cn(
  buttonVariants({ variant: 'ghost' }),
  'h-10 gap-2 rounded-full border border-transparent px-3.5 text-sm font-medium text-muted-foreground',
  // ghost 의 dark:hover 색보다 앞서도록 dark: 를 붙이고, 현재 항목 배경은 hover 중에도 유지한다.
  'hover:text-foreground dark:hover:bg-surface-2',
  'data-[status=active]:border-border data-[status=active]:bg-surface-3! data-[status=active]:text-foreground',
);

function DockItem({ icon: Icon, label }: { icon: LucideIcon; label: string }) {
  return (
    <>
      <Icon className="size-4 opacity-85 in-data-[status=active]:text-point-soft in-data-[status=active]:opacity-100" />
      <span>{label}</span>
    </>
  );
}

// 순서는 작업 관리 → 실시간 관제 → CCTV 관제다(첫 화면이 작업 관리, S15P31A101-665).
// 상단 바에서 현재 화면만 배경과 테두리로 강조한다.
export function Dock() {
  // 오늘 날짜 화면을 열어 둔 채 나왔으면 작업 관리가 그 화면으로 돌아온다. 어제 날짜가 남았으면(자정이 지났으면) 달력으로 간다.
  const lastDayDate = useWorkNavStore((s) => s.lastDayDate);
  const workDate = lastDayDate === toDateKey(new Date()) ? lastDayDate : undefined;

  return (
    <nav aria-label="주 메뉴" className="flex items-center gap-1">
      <Link
        to="/work"
        search={workDate === undefined ? undefined : { date: workDate }}
        aria-label="작업 관리"
        className={ITEM_CLASS}
      >
        <DockItem icon={FileText} label="작업 관리" />
      </Link>
      <Link to="/live" aria-label="실시간 3D 관제" className={ITEM_CLASS}>
        <DockItem icon={Monitor} label="실시간 3D 관제" />
      </Link>
      <Link to="/cctv" aria-label="CCTV 관제" className={ITEM_CLASS}>
        <DockItem icon={Video} label="CCTV 관제" />
      </Link>
    </nav>
  );
}
