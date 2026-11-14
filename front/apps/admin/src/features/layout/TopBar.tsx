import { ROLE_LABEL } from '@/features/auth/roleLabel';
import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { useLogout } from '@/features/auth/useLogout';
import { SoundButton } from '@/features/events/SoundButton';
import { GpuIndicator } from '@/features/gpu/GpuIndicator';
import { GpuSettingsDialog } from '@/features/gpu/GpuSettingsDialog';
import { BrandMark, Wordmark } from '@/features/layout/BrandMark';
import { clockLabel } from '@/features/layout/status';
import { useThemeStore } from '@/features/layout/theme.store';
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
import { ChevronDown, Cpu, LogOut, Moon, Sun } from 'lucide-react';
import { useState } from 'react';

// 상단 바는 로고·GPU 점(S15P31A101-702: 점 + 짧은 글자, 판정 지연은 그 마우스 올림 정보창 한 줄)·시계·경보 소리 버튼(확인 권한이 있는
// 계정만)·관리자 메뉴(규칙 기준 선택은 교실 시험 고정으로 뺌, -669. 관리자에게만 「GPU 설정」)만 둔다(관제 데모와 같다).
// 상태 값(System Health·데이터 연령·카메라·보정·구성·장면)은 시스템 상태 화면에서 본다.

const NONE = '—';

function Ec2Clock() {
  const now = useNow();
  const offset = useRealtimeStore((s) => s.health?.clockOffsetMs);
  return (
    <span className="font-mono text-sm whitespace-nowrap">{clockLabel(offset, now) ?? NONE}</span>
  );
}

function AccountMenu() {
  const isDark = useThemeStore((s) => s.theme === 'dark');
  const toggleTheme = useThemeStore((s) => s.toggle);
  const role = useCurrentRole();
  const logout = useLogout();
  const [gpuSettings, setGpuSettings] = useState(false);

  return (
    <>
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
          {role === 'ADMIN' && (
            <DropdownMenuItem onClick={() => setGpuSettings(true)}>
              <Cpu />
              GPU 설정
            </DropdownMenuItem>
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={() => logout()}>
            <LogOut />
            로그아웃
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {gpuSettings && <GpuSettingsDialog onClose={() => setGpuSettings(false)} />}
    </>
  );
}

export function TopBar({ floating }: { floating: boolean }) {
  return (
    <header
      className={cn(
        'flex h-[60px] items-center gap-x-5 gap-y-3 bg-card px-5',
        floating && 'bg-card/78 shadow-[0_1px_0_var(--border)] backdrop-blur-[8px]',
      )}
    >
      <Link to="/work" className="flex items-center gap-2.5">
        <BrandMark />
        <Wordmark />
      </Link>
      <div className="ml-auto flex items-center gap-x-3">
        <GpuIndicator />
        <Ec2Clock />
      </div>
      <SoundButton />
      <AccountMenu />
    </header>
  );
}
