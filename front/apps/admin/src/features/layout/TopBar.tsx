import { ROLE_LABEL } from '@/features/auth/roleLabel';
import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { useLogout } from '@/features/auth/useLogout';
import { SoundButton } from '@/features/events/SoundButton';
import { BrandMark, Wordmark } from '@/features/layout/BrandMark';
import { clockLabel } from '@/features/layout/status';
import { useThemeStore } from '@/features/layout/theme.store';
import { useNow } from '@/features/layout/useNow';
import { useTier0Connected } from '@/hooks/useTier0Connected';
import { isLagHigh, LAG_WINDOW_MS } from '@/lib/tier0Lag';
import { selectTier0LagMs, useRealtimeStore } from '@/store/realtime.store';
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

// 상단 바는 로고·판정 지연(GPU 가 이어졌을 때만)·시계·경보 소리 버튼(확인 권한이 있는 계정만)·관리자 메뉴(규칙 기준 선택은 교실 시험 고정으로 뺌, -669)만
// 둔다(관제 데모와 같다). 상태 값(System Health·데이터 연령·카메라·보정·구성·장면)은 시스템 상태 화면에서 본다.

const NONE = '—';

function Ec2Clock() {
  const now = useNow();
  const offset = useRealtimeStore((s) => s.health?.clockOffsetMs);
  return (
    <span className="font-mono text-sm whitespace-nowrap">{clockLabel(offset, now) ?? NONE}</span>
  );
}

/**
 * 판정 지연(S15P31A101-665, 이름은 -704). Tier 0 가 자기 시계로 잰 T_lag 의 최근 몇 초 평균이다(lib/tier0Lag).
 * Tier 0(GPU)가 이어지지 않았거나(꺼 둔 정상 상태 포함, EC2 와 끊김) Tier 0 가 모른다고 하면 숨긴다(지어내지 않음).
 * 기준(명세 6.1 DEGRADED) 이상이면 지연 색이다. 카메라별 값은 CCTV 칸 오른쪽 위 배지가 보인다.
 */
function Tier0Lag() {
  const connected = useTier0Connected();
  const lag = useRealtimeStore(selectTier0LagMs);
  if (!connected || lag === undefined) return null;
  return (
    <span
      title={`Tier 0 가 잰 촬영→판정 처리 지연, 최근 ${LAG_WINDOW_MS / 1000}초 평균`}
      className={cn(
        'font-mono text-sm whitespace-nowrap',
        isLagHigh(lag) ? 'font-semibold text-delay' : 'text-muted-foreground',
      )}
    >
      판정 지연 {lag} ms
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
        <Tier0Lag />
        <Ec2Clock />
      </div>
      <SoundButton />
      <AccountMenu />
    </header>
  );
}
