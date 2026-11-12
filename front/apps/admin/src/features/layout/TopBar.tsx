import { ROLE_LABEL } from '@/features/auth/roleLabel';
import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { useLogout } from '@/features/auth/useLogout';
import { SoundButton } from '@/features/events/SoundButton';
import { BrandMark, Wordmark } from '@/features/layout/BrandMark';
import { clockLabel } from '@/features/layout/status';
import { useThemeStore } from '@/features/layout/theme.store';
import { useCurrentSite } from '@/features/layout/useCurrentSite';
import { useNow } from '@/features/layout/useNow';
import { useRealtimeStore } from '@/store/realtime.store';
import { Button } from '@simai/ui/components/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@simai/ui/components/dropdown-menu';
import { cn } from '@simai/ui/lib/utils';
import { Link } from '@tanstack/react-router';
import { ChevronDown, LogOut, Moon, Sun } from 'lucide-react';

// 상단 바는 로고·현장명·시계·경보 소리 버튼(확인 권한이 있는 계정만)·관리자 메뉴만 둔다(관제 데모와 같다). 상태 값(System Health·데이터 연령·카메라·보정·구성·장면)은
// 시스템 상태 화면에서 본다.

const NONE = '—';

function Ec2Clock() {
  const now = useNow();
  const offset = useRealtimeStore((s) => s.health?.clockOffsetMs);
  return (
    <span className="ml-auto font-mono text-sm whitespace-nowrap">
      {clockLabel(offset, now) ?? NONE}
    </span>
  );
}

function AccountMenu() {
  const isDark = useThemeStore((s) => s.theme === 'dark');
  const toggleTheme = useThemeStore((s) => s.toggle);
  const role = useCurrentRole();
  const logout = useLogout();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={<Button variant="secondary" size="sm" className="text-muted-foreground" />}
      >
        {role ? ROLE_LABEL[role] : NONE}
        <ChevronDown aria-hidden />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[220px]">
        <DropdownMenuItem onClick={toggleTheme}>
          {isDark ? <Sun /> : <Moon />}
          {isDark ? '화이트 모드' : '다크 모드'}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => logout()}>
          <LogOut />
          로그아웃
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function TopBar({ floating }: { floating: boolean }) {
  const site = useCurrentSite();
  return (
    <header
      className={cn(
        'flex h-[60px] items-center gap-x-5 gap-y-3 bg-card px-5',
        floating && 'bg-card/78 shadow-[0_1px_0_var(--border)] backdrop-blur-[8px]',
      )}
    >
      <Link to="/" className="flex items-center gap-2.5">
        <BrandMark />
        <Wordmark />
      </Link>
      <span className="text-[13px] whitespace-nowrap text-muted-foreground">{site?.name}</span>
      <Ec2Clock />
      <SoundButton />
      <AccountMenu />
    </header>
  );
}
