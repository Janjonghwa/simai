import { ROLE_LABEL } from '@/features/auth/roleLabel';
import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { useLogout } from '@/features/auth/useLogout';
import { BrandMark } from '@/features/layout/BrandMark';
import {
  calibrationLabel,
  cameraLabel,
  chipState,
  clockLabel,
  configLabel,
  dataAgeSec,
  sceneLabel,
} from '@/features/layout/status';
import { SystemHealthChip } from '@/features/layout/SystemHealthChip';
import { useThemeStore } from '@/features/layout/theme.store';
import { useLinkAlive } from '@/features/layout/useLinkAlive';
import { useNow } from '@/features/layout/useNow';
import { useRealtimeStore, type HealthSnapshot } from '@/store/realtime.store';
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
import { ChevronDown, LogOut, Moon, Sun } from 'lucide-react';

// 값마다 작은 컴포넌트가 실시간 저장소를 직접 구독한다. health 가 1초마다 와도 바뀐 칸만 다시 그린다.

const NONE = '—';

function Meta({ label, value, late }: { label: string; value?: string; late?: boolean }) {
  return (
    <span className="whitespace-nowrap">
      {label}{' '}
      <b className={cn('font-mono font-medium text-muted-foreground', late && 'text-delay')}>
        {value ?? NONE}
      </b>
    </span>
  );
}

/** health 에서 문구 하나를 보인다. 연결이 끊기면 지금 값을 모르므로 비운다. */
function HealthMeta({
  label,
  alive,
  select,
}: {
  label: string;
  alive: boolean;
  select: (health: HealthSnapshot | null) => string | undefined;
}) {
  const value = useRealtimeStore((s) => select(s.health));
  return <Meta label={label} value={alive ? value : undefined} />;
}

/** 데이터 연령. 끊겨도 마지막으로 받은 때부터 계속 늘어나고, 지연 색으로 바뀐다. */
function DataAgeMeta({ alive }: { alive: boolean }) {
  const now = useNow();
  const age = useRealtimeStore((s) => dataAgeSec(s.health, now));
  return (
    <Meta
      label="데이터"
      value={age === undefined ? undefined : `${age.toFixed(1)}초`}
      late={!alive && age !== undefined}
    />
  );
}

function HealthStatus({ alive }: { alive: boolean }) {
  const state = useRealtimeStore((s) => chipState(s, alive));
  return <SystemHealthChip state={state} />;
}

function Ec2Clock() {
  const now = useNow();
  const offset = useRealtimeStore((s) => s.health?.clockOffsetMs);
  return (
    <span className="font-mono text-sm whitespace-nowrap">{clockLabel(offset, now) ?? NONE}</span>
  );
}

function AccountMenu() {
  const theme = useThemeStore((s) => s.theme);
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
          {theme === 'dark' ? <Sun /> : <Moon />}
          {theme === 'dark' ? '화이트 모드' : '다크 모드'}
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

const Divider = () => <Separator orientation="vertical" className="h-3" aria-hidden />;

export function TopBar({ floating }: { floating: boolean }) {
  // 끊김 여부가 바뀔 때만 다시 그린다(값들은 아래 칸이 각자 구독한다).
  const alive = useLinkAlive();

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
      <HealthStatus alive={alive} />
      <div className="flex min-w-0 flex-1 items-center gap-2.5 overflow-hidden text-[13px] text-faint">
        <DataAgeMeta alive={alive} />
        <Divider />
        <HealthMeta label="카메라" alive={alive} select={cameraLabel} />
        <Divider />
        <HealthMeta label="보정" alive={alive} select={calibrationLabel} />
        <Divider />
        <HealthMeta label="구성" alive={alive} select={configLabel} />
        <Divider />
        <HealthMeta label="장면" alive={alive} select={sceneLabel} />
      </div>
      <Ec2Clock />
      <AccountMenu />
    </header>
  );
}
