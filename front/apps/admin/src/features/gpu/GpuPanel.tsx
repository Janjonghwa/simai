import { GpuInfoLines } from '@/features/gpu/GpuInfoLines';
import { GpuLaunchForm } from '@/features/gpu/GpuLaunchForm';
import {
  activeGpuProvider,
  formatElapsed,
  formatUsd,
  GPU_PROVIDER_LABEL,
  GPU_PROVIDER_TABS,
  gpuErrorMessage,
  gpuSummary,
  isGpuOn,
  STOP_BLOCK_TEXT,
  STOP_RELAUNCH_TIME_NOTE,
} from '@/features/gpu/gpuStatus';
import type { GpuProviderId } from '@/features/gpu/types';
import { useGpuStatus, useGpuStop } from '@/features/gpu/useGpu';
import { useNow } from '@/features/layout/useNow';
import { ModalDialog } from '@/features/tbm/ModalDialog';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useRef, useState, type KeyboardEvent } from 'react';

// GPU 켜기·끄기 패널(S15P31A101-702, -733). 상단바 GPU 점을 누르면 열린다.
// 맨 위 공급자 탭 「AWS」「RunPod」(S15P31A101-841): 처음은 AWS 탭(켜져 있거나 켜는 중이면 그 GPU 의 공급자 탭).
// 꺼져 있으면 탭 공급자의 재고 표(GpuLaunchForm), 켜져 있거나 켜는 중이면 요약과 「GPU 끄기」. 관제(LIVE)·세팅 중이면 끄기를 막고 까닭을 보인다.
// 끄기는 확인을 한 번 더 받으며, 다시 켤 때 예상 시간과 이번 세션 비용을 함께 알린다.

const TAB = 'h-7 rounded-md px-2.5 text-sm font-semibold';
const TAB_ON = 'bg-primary text-primary-foreground';
const TAB_OFF = 'text-muted-foreground hover:bg-surface-3';

export function GpuPanel({ onClose }: { onClose: () => void }) {
  const { status } = useGpuStatus();
  const on = isGpuOn(status);
  const onProvider = activeGpuProvider(status);
  const [chosen, setChosen] = useState<GpuProviderId | null>(null);
  const tab = chosen ?? onProvider ?? 'AWS';

  let body;
  if (on && (onProvider === null || onProvider === tab)) {
    body = <GpuStopView onClose={onClose} />;
  } else if (on && onProvider) {
    body = (
      <OtherProviderOn
        onProvider={onProvider}
        onShow={() => setChosen(onProvider)}
        onClose={onClose}
      />
    );
  } else {
    body = (
      <GpuLaunchForm
        key={tab}
        provider={tab}
        onLaunched={onClose}
        onCancel={onClose}
        onSwitchProvider={setChosen}
      />
    );
  }

  return (
    <ModalDialog label="GPU" onClose={onClose} className="w-[min(880px,calc(100vw-32px))]">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-5 py-3">
        <h2 className="text-base font-semibold">{on ? 'GPU' : 'GPU 켜기'}</h2>
        <ProviderTabs value={tab} onChange={setChosen} />
      </div>
      <div role="tabpanel" id="gpu-provider-panel" aria-labelledby={`gpu-provider-tab-${tab}`}>
        {body}
      </div>
    </ModalDialog>
  );
}

/** 공급자 탭(WAI-ARIA tabs: 고른 탭만 Tab 키로 닿고, 좌우·Home·End 키로 옮기면 바로 고른다) */
function ProviderTabs({
  value,
  onChange,
}: {
  value: GpuProviderId;
  onChange: (provider: GpuProviderId) => void;
}) {
  const refs = useRef<Partial<Record<GpuProviderId, HTMLButtonElement | null>>>({});
  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = GPU_PROVIDER_TABS.indexOf(value);
    const n = GPU_PROVIDER_TABS.length;
    const next =
      e.key === 'ArrowRight'
        ? GPU_PROVIDER_TABS[(i + 1) % n]
        : e.key === 'ArrowLeft'
          ? GPU_PROVIDER_TABS[(i - 1 + n) % n]
          : e.key === 'Home'
            ? GPU_PROVIDER_TABS[0]
            : e.key === 'End'
              ? GPU_PROVIDER_TABS[n - 1]
              : undefined;
    if (!next) return;
    e.preventDefault();
    onChange(next);
    refs.current[next]?.focus();
  };
  return (
    <div role="tablist" aria-label="GPU 공급자" className="flex items-center gap-1">
      {GPU_PROVIDER_TABS.map((p) => (
        <button
          key={p}
          ref={(el) => {
            refs.current[p] = el;
          }}
          type="button"
          role="tab"
          id={`gpu-provider-tab-${p}`}
          aria-selected={p === value}
          aria-controls="gpu-provider-panel"
          tabIndex={p === value ? 0 : -1}
          className={cn(TAB, p === value ? TAB_ON : TAB_OFF)}
          onClick={() => onChange(p)}
          onKeyDown={onKeyDown}
        >
          {GPU_PROVIDER_LABEL[p]}
        </button>
      ))}
    </div>
  );
}

/** 다른 공급자의 GPU 가 켜져 있을 때 이 탭에 보이는 안내(한 시점에 GPU 하나, 명세 3.12) */
function OtherProviderOn({
  onProvider,
  onShow,
  onClose,
}: {
  onProvider: GpuProviderId;
  onShow: () => void;
  onClose: () => void;
}) {
  return (
    <div className="flex flex-col gap-3 p-5" data-testid="gpu-other-provider-on">
      <p role="status" className="text-sm">
        지금 {GPU_PROVIDER_LABEL[onProvider]} GPU 가 켜져 있어 다른 공급자로는 켤 수 없습니다. GPU
        는 한 번에 하나만 켭니다.
      </p>
      <div className="flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose}>
          닫기
        </Button>
        <Button onClick={onShow}>{GPU_PROVIDER_LABEL[onProvider]} 탭 보기</Button>
      </div>
    </div>
  );
}

function GpuStopView({ onClose }: { onClose: () => void }) {
  const { status, clockOffsetMs } = useGpuStatus();
  const now = useNow();
  const summary = gpuSummary(status, now + clockOffsetMs);
  const { stop, pending, error } = useGpuStop();
  const [confirming, setConfirming] = useState(false);
  const block = status?.stop_block ?? null;

  return (
    <div className="flex flex-col gap-4 p-5">
      <GpuInfoLines />
      {block && (
        <p role="status" className="text-sm">
          {STOP_BLOCK_TEXT[block]}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm">
          {gpuErrorMessage(error)}
        </p>
      )}
      {confirming ? (
        <div className="flex flex-col gap-3" data-testid="gpu-stop-confirm">
          <p className="text-sm font-semibold">GPU 를 끌까요? {STOP_RELAUNCH_TIME_NOTE}</p>
          {summary && (
            <p className="text-sm text-muted-foreground">
              {summary.costUsd !== null
                ? `이번 세션 비용: 약 ${formatUsd(summary.costUsd)} (경과 ${formatElapsed(summary.elapsedMs)}${summary.pricePerHourUsd !== null ? ` · ${formatUsd(summary.pricePerHourUsd)}/h` : ''})`
                : `이번 세션 경과: ${formatElapsed(summary.elapsedMs)}`}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setConfirming(false)} disabled={pending}>
              취소
            </Button>
            <Button
              variant="destructive"
              onClick={() => stop(undefined, { onSuccess: onClose })}
              disabled={pending}
            >
              {pending ? '끄는 중…' : '끄기'}
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            닫기
          </Button>
          <Button
            variant="destructive"
            disabled={block !== null}
            onClick={() => setConfirming(true)}
          >
            GPU 끄기
          </Button>
        </div>
      )}
    </div>
  );
}
