import { BrandMark } from '@/features/layout/BrandMark';
import type { StatusValues } from '@/features/layout/status';
import { SystemHealthChip } from '@/features/layout/SystemHealthChip';
import { useThemeStore } from '@/features/layout/theme.store';
import { Button } from '@simai/ui/components/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@simai/ui/components/dropdown-menu';
import { Separator } from '@simai/ui/components/separator';
import { cn } from '@simai/ui/lib/utils';
import { Link } from '@tanstack/react-router';
import { LogOut, Moon, Sun } from 'lucide-react';

const NONE = '—';

function Meta({ label, value }: { label: string; value?: string }) {
  return (
    <span className="whitespace-nowrap">
      {label} <b className="font-mono font-medium text-muted-foreground">{value ?? NONE}</b>
    </span>
  );
}

export function TopBar({ status, floating }: { status: StatusValues; floating: boolean }) {
  const theme = useThemeStore((s) => s.theme);
  const toggleTheme = useThemeStore((s) => s.toggle);
  const cameras = status.cameras && `${status.cameras.online}/${status.cameras.total}`;
  const dataAge = status.dataAgeSec === undefined ? undefined : `${status.dataAgeSec.toFixed(1)}초`;

  return (
    <header
      className={cn(
        'flex h-[60px] items-center gap-x-5 gap-y-3 bg-card px-5',
        floating && 'bg-card/78 shadow-[0_1px_0_var(--border)] backdrop-blur-[8px]',
      )}
    >
      <Link to="/" className="flex items-center gap-2.5">
        <BrandMark />
        <strong className="text-[21px] font-bold tracking-[-0.4px]">
          sim<span className="text-primary">AI</span>
        </strong>
      </Link>
      <span className="text-[13px] whitespace-nowrap text-muted-foreground">
        교실 테스트베드 · 10×10m
      </span>
      <SystemHealthChip health={status.health} />
      <div className="flex min-w-0 flex-1 items-center gap-2.5 overflow-hidden text-[13px] text-faint">
        <Meta label="데이터" value={dataAge} />
        <Separator orientation="vertical" className="h-3" />
        <Meta label="카메라" value={cameras} />
        <Separator orientation="vertical" className="h-3" />
        <Meta label="보정" value={status.calibration} />
        <Separator orientation="vertical" className="h-3" />
        <Meta label="구성" value={status.config} />
        <Separator orientation="vertical" className="h-3" />
        <Meta label="장면" value={status.scene} />
      </div>
      <span className="font-mono text-sm whitespace-nowrap">{status.clock ?? NONE}</span>
      <DropdownMenu>
        <DropdownMenuTrigger
          render={<Button variant="secondary" size="sm" className="text-muted-foreground" />}
        >
          관리자 ▾
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-[220px]">
          <DropdownMenuItem onClick={toggleTheme}>
            {theme === 'dark' ? <Sun /> : <Moon />}
            {theme === 'dark' ? '화이트 모드' : '다크 모드'}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem render={<Link to="/login" />}>
            <LogOut />
            로그아웃
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </header>
  );
}
