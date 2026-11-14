import type { SetupMode } from '@/features/setup/types';
import type { ReuseChoiceState } from '@/features/setup/useReuseScene';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import type { ReactNode } from 'react';

// 1단계 「맵」 방식 고르기(S15P31A101-689). 예전 「3D 공간」 창(세션이 없을 때만 뜸) 대신 1단계 안에 늘 둔다: 실패·취소한 시도가 있어도,
// 새로고침 뒤에도 같은 자리에서 고른다. 이전 맵 재사용이 기본이고, 쓸 수 없으면 꺼서 이유를 두 줄(무엇이/어떻게)로 보인다.

/** 재사용은 맵을 굽지 않아 보정만 다시 한다(사용자 기준 약 1~2분), 새 맵은 굽기까지 약 13분(15차 실측). */
const REUSE_ETA = '기본 · 약 1~2분';
const NEW_ETA = '약 13분';

const OPTION =
  'flex min-h-16 flex-1 flex-col items-start justify-center gap-1 rounded-lg border px-3.5 py-2.5 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed';
const SELECTED = 'border-primary bg-primary/10 ring-[1.5px] ring-primary ring-inset';
const IDLE = 'border-border bg-card enabled:hover:bg-surface-2';

function Option({
  label,
  eta,
  selected,
  disabled,
  onSelect,
  children,
}: {
  label: string;
  eta: string;
  selected: boolean;
  disabled: boolean;
  onSelect: () => void;
  children?: ReactNode;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      disabled={disabled}
      onClick={onSelect}
      className={cn(OPTION, selected ? SELECTED : IDLE, disabled && 'opacity-60')}
    >
      <span className="text-sm font-semibold">{label}</span>
      <span className="text-xs text-muted-foreground tabular-nums">{eta}</span>
      {children}
    </button>
  );
}

export function SetupModeChoice({
  mode,
  onChange,
  reuse,
}: {
  /** 지금 고른(또는 기본) 방식. 재사용을 쓸 수 없으면 NEW 다. */
  mode: SetupMode;
  onChange: (mode: SetupMode) => void;
  reuse: ReuseChoiceState;
}) {
  const reuseDisabled = reuse.checking || reuse.blocked !== null;
  return (
    <Card size="sm" className="shrink-0">
      <CardHeader>
        <CardTitle>
          <h2>맵</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div role="radiogroup" aria-label="맵 방식" className="flex gap-2">
          <Option
            label="이전 맵 재사용"
            eta={reuse.checking ? '확인 중' : REUSE_ETA}
            selected={mode === 'REUSE'}
            disabled={reuseDisabled}
            onSelect={() => onChange('REUSE')}
          >
            {reuse.blocked && (
              <span role="note" aria-label="이전 맵을 쓸 수 없는 이유" className="text-xs">
                <span className="block font-semibold text-delay">{reuse.blocked.what}</span>
                <span className="block text-muted-foreground">{reuse.blocked.how}</span>
              </span>
            )}
            {!reuse.blocked && reuse.note && (
              <span className="text-xs text-muted-foreground">{reuse.note}</span>
            )}
          </Option>
          <Option
            label="새 맵 만들기"
            eta={NEW_ETA}
            selected={mode === 'NEW'}
            disabled={false}
            onSelect={() => onChange('NEW')}
          />
        </div>
      </CardContent>
    </Card>
  );
}
