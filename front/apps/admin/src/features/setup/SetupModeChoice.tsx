import type { SetupMode } from '@/features/setup/types';
import type { ReuseChoiceState, ReuseMapOption } from '@/features/setup/useReuseScene';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { FolderOpen, MapPlus } from 'lucide-react';
import type { ReactNode } from 'react';

// 1단계 「맵」 방식 고르기(S15P31A101-689). 예전 「3D 공간」 창(세션이 없을 때만 뜸) 대신 1단계 안에 늘 둔다: 실패·취소한 시도가 있어도,
// 새로고침 뒤에도 같은 자리에서 고른다. 새 맵 만들기가 기본이고(S15P31A101-762: 카메라 각도가 바뀌어 있을 위험이 높아 재사용은 고를 때만 쓴다),
// 이전 맵 재사용을 쓸 수 없으면 꺼서 이유를 두 줄(무엇이/어떻게)로 보인다.
// S15P31A101-714: 재사용 안에서 최근 완료 맵을 날짜와 함께 고른다. 재사용을 고르면 지금 카메라와 가장 잘 맞는 맵(추천)이 먼저 골라지고, 맞지 않는 맵은 이유와 함께 꺼진다.
// 재사용은 그 맵을 만들 때의 보정·카메라 높이·구역을 그대로 쓴다(입력한 높이는 쓰지 않는다).

const NEW_ETA = '약 10분 소요';

const OPTION = 'flex min-w-0 flex-col rounded-lg border text-left';
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
  eta?: string;
  selected: boolean;
  disabled: boolean;
  onSelect: () => void;
  children?: ReactNode;
}) {
  return (
    <div
      className={cn(
        OPTION,
        selected ? SELECTED : IDLE,
        selected && 'min-h-0',
        selected && label === '새 맵 만들기' && 'flex-1',
        disabled && 'opacity-60',
      )}
    >
      <button
        type="button"
        role="radio"
        aria-checked={selected}
        disabled={disabled}
        onClick={onSelect}
        className="flex w-full shrink-0 items-center gap-1.5 rounded-lg px-3 py-3 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed"
      >
        <span className="flex w-full items-center gap-1.5">
          <span aria-hidden className="shrink-0">
            {label === '새 맵 만들기' ? <MapPlus size={20} /> : <FolderOpen size={20} />}
          </span>
          <span className="text-[13px] font-semibold whitespace-nowrap">{label}</span>
          {eta && (
            <span className="ml-auto rounded border border-border bg-surface-2 px-1 py-0.5 text-xs whitespace-nowrap text-muted-foreground tabular-nums">
              {eta}
            </span>
          )}
        </span>
      </button>
      {selected && children}
    </div>
  );
}

/** 최근 완료 맵 목록(날짜·상태). 고를 수 없는 맵은 꺼지고 이유를 보인다. */
function MapList({
  maps,
  selectedId,
  onSelect,
  disabled,
}: {
  maps: readonly ReuseMapOption[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  disabled: boolean;
}) {
  return (
    <div
      role="radiogroup"
      aria-label="재사용할 맵"
      className="flex min-h-0 flex-col gap-1.5 overflow-y-auto"
    >
      {maps.map((map) => {
        const selected = map.id === selectedId;
        return (
          <button
            key={map.id}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={disabled || !map.selectable}
            onClick={() => onSelect(map.id)}
            className={cn(
              'flex shrink-0 flex-col items-start gap-0.5 rounded-md border px-3 py-2 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed',
              selected ? SELECTED : IDLE,
              !map.selectable && 'opacity-60',
            )}
          >
            <span className="flex items-center gap-2 text-sm font-semibold tabular-nums">
              {map.dateLabel}
              {map.recommended && (
                <span className="rounded bg-primary px-1.5 py-0.5 text-[11px] font-semibold text-primary-foreground">
                  추천
                </span>
              )}
            </span>
            {!map.selectable && (
              <span className="text-xs font-semibold text-delay">
                지금 카메라 보정과 맞지 않아요
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export function SetupModeChoice({
  mode,
  onChange,
  reuse,
  mapId = null,
  onMapChange,
}: {
  /** 지금 고른(또는 기본) 방식. 재사용을 쓸 수 없으면 NEW 다. */
  mode: SetupMode;
  onChange: (mode: SetupMode) => void;
  reuse: ReuseChoiceState;
  /** 재사용으로 시작할 맵(고른 맵, 없으면 추천). 맞는 맵이 없으면 null(S15P31A101-714). */
  mapId?: string | null;
  onMapChange?: (id: string) => void;
}) {
  const reuseDisabled = reuse.checking || reuse.blocked !== null;
  return (
    <Card size="sm" data-map-choice className="flex min-h-0 flex-1 flex-col">
      <CardHeader>
        <CardTitle>
          <h2>맵</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-2">
        <div role="radiogroup" aria-label="맵 방식" className="flex min-h-0 flex-1 flex-col gap-2">
          <Option
            label="새 맵 만들기"
            eta={NEW_ETA}
            selected={mode === 'NEW'}
            disabled={false}
            onSelect={() => onChange('NEW')}
          >
            <div className="flex flex-1 flex-col px-3 pb-3">
              <p className="text-xs leading-relaxed text-muted-foreground">
                카메라 4대로 지금 현장을 새로 찍어 3D 맵을 만들어요.
              </p>
              {/* 세 단계를 카드 남은 높이에 고르게 펼친다: 앞 두 줄이 남는 높이를 나눠 갖고 마지막 줄은 아래에 붙는다(위에 몰리지 않게). */}
              <ol className="mt-4 flex max-h-72 flex-1 flex-col pb-1 text-xs">
                {[
                  ['배경 촬영', '약 20초', '카메라 앞을 비워 주세요'],
                  ['카메라 확인', '약 30초', '한 사람이 방 안을 걸어요'],
                  ['3D 맵 만들기', '약 9분', '기다리는 동안 구역·TBM 확인'],
                ].map(([name, time, action], index) => (
                  <li
                    key={name}
                    className={cn(
                      'relative flex min-h-12 items-start gap-2',
                      index < 2 && 'flex-1',
                    )}
                  >
                    {index < 2 && (
                      <span
                        aria-hidden
                        className="absolute top-5 bottom-0 left-[9px] w-px bg-primary/25"
                      />
                    )}
                    <span className="relative flex size-5 shrink-0 items-center justify-center rounded-full border border-primary/40 bg-surface-2 text-muted-foreground tabular-nums">
                      {index + 1}
                    </span>
                    <span className="flex min-w-0 flex-col pt-0.5">
                      <span className="flex items-baseline gap-1">
                        <span className="font-semibold">{name}</span>
                        <span className="whitespace-nowrap text-muted-foreground tabular-nums">
                          · {time}
                        </span>
                      </span>
                      <span className="text-muted-foreground">{action}</span>
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          </Option>
          <Option
            label="이전 맵 재사용"
            selected={mode === 'REUSE'}
            disabled={reuseDisabled}
            onSelect={() => onChange('REUSE')}
          >
            <div className="flex min-h-0 flex-col gap-2 px-3 pb-3">
              <p className="text-xs leading-relaxed text-muted-foreground">
                카메라 자리가 그대로면 지난 맵과 보정·구역을 그대로 써요.
              </p>
              {reuse.maps.length > 0 && (
                <MapList
                  maps={reuse.maps}
                  selectedId={mapId}
                  disabled={false}
                  onSelect={(id) => onMapChange?.(id)}
                />
              )}
            </div>
          </Option>
        </div>
        {reuse.blocked && (
          <span role="note" aria-label="이전 맵을 쓸 수 없는 이유" className="text-xs">
            <span className="block font-semibold text-delay">{reuse.blocked.what}</span>
            <span className="block text-muted-foreground">{reuse.blocked.how}</span>
          </span>
        )}
      </CardContent>
    </Card>
  );
}
