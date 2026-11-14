import { formatUsd, gpuErrorMessage, STOCK_LABEL } from '@/features/gpu/gpuStatus';
import type { GpuOfferCell, GpuOffers } from '@/features/gpu/types';
import { useGpuLaunch, useGpuOffers } from '@/features/gpu/useGpu';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useState } from 'react';

// GPU 켜기(S15P31A101-702): 공급자의 지금 재고·시간당 가격을 GPU 종류 × 지역 표로 보이고, 기본은 서버가 고른 선호 순서 첫 재고다.
// 칸을 눌러 바꿀 수 있다(재고 없음·가격 상한 밖은 못 고름). 기본과 다른 GPU 종류면 엔진 캐시 안내를 둔다. 「켜기」는 확인을 한 번 더 받는다.

interface Pick {
  gpuType: string;
  region: string;
}

const ENGINE_NOTE = '엔진 캐시가 GPU 종류별이라 첫 실행은 엔진 빌드로 몇 분 더 걸립니다.';

function cellOf(offers: GpuOffers, pick: Pick): GpuOfferCell | undefined {
  return offers.cells.find((c) => c.gpu_type === pick.gpuType && c.region === pick.region);
}

function nameOf(offers: GpuOffers, gpuType: string): string {
  return offers.gpu_types.find((g) => g.gpu_type === gpuType)?.name ?? gpuType;
}

export function GpuLaunchForm({
  onLaunched,
  onCancel,
}: {
  onLaunched: () => void;
  onCancel: () => void;
}) {
  const { offers, pending, error: offersError } = useGpuOffers(true);
  const { launch, pending: launching, error: launchError } = useGpuLaunch();
  const [picked, setPicked] = useState<Pick | null>(null);
  const [confirming, setConfirming] = useState(false);

  if (pending) return <p className="p-5 text-sm text-muted-foreground">재고를 확인하는 중…</p>;
  if (offersError || !offers) {
    return (
      <div className="flex flex-col gap-3 p-5">
        <p role="alert" className="text-sm">
          {gpuErrorMessage(offersError)}
        </p>
        <div className="flex justify-end">
          <Button variant="secondary" onClick={onCancel}>
            닫기
          </Button>
        </div>
      </div>
    );
  }

  const fallback = offers.default
    ? { gpuType: offers.default.gpu_type, region: offers.default.region }
    : null;
  const pick = picked ?? fallback;
  const cell = pick ? cellOf(offers, pick) : undefined;
  const baseGpu = offers.default?.gpu_type ?? offers.gpu_types[0]?.gpu_type;
  const otherGpu = pick !== null && baseGpu !== undefined && pick.gpuType !== baseGpu;
  const usable = (c: GpuOfferCell) => c.stock !== 'NONE' && c.within_cap;

  const submit = () => {
    if (!pick) return;
    // 기본 그대로면 서버가 고르게 비운다(그 사이 재고가 바뀌어도 선호 순서로 다시 고른다)
    const body = picked === null ? {} : { gpu_type: pick.gpuType, region: pick.region };
    launch(body, { onSuccess: onLaunched });
  };

  if (confirming && pick) {
    return (
      <div className="flex flex-col gap-4 p-5" data-testid="gpu-launch-confirm">
        <p className="text-sm font-semibold">GPU 를 켤까요?</p>
        <p className="text-sm">
          {nameOf(offers, pick.gpuType)} · {pick.region}
          {cell?.price_per_hour_usd != null && ` · 시간당 ${formatUsd(cell.price_per_hour_usd)}`}
        </p>
        {otherGpu && <p className="text-sm text-muted-foreground">{ENGINE_NOTE}</p>}
        {launchError && (
          <p role="alert" className="text-sm">
            {gpuErrorMessage(launchError)}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setConfirming(false)} disabled={launching}>
            취소
          </Button>
          <Button onClick={submit} disabled={launching}>
            {launching ? '켜는 중…' : '켜기'}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 p-5">
      <div className="overflow-x-auto">
        <table className="w-full border-separate border-spacing-1 text-sm" aria-label="GPU 재고">
          <thead>
            <tr>
              <th className="text-left font-normal text-muted-foreground" />
              {offers.regions.map((r) => (
                <th
                  key={r.region}
                  scope="col"
                  className="px-2 text-left font-normal text-muted-foreground"
                >
                  {r.region}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {offers.gpu_types.map((g) => (
              <tr key={g.gpu_type}>
                <th scope="row" className="pr-2 text-left font-medium whitespace-nowrap">
                  {g.name}
                </th>
                {offers.regions.map((r) => {
                  const c = cellOf(offers, { gpuType: g.gpu_type, region: r.region });
                  const selected = pick?.gpuType === g.gpu_type && pick.region === r.region;
                  const ok = c !== undefined && usable(c);
                  return (
                    <td key={r.region}>
                      <button
                        type="button"
                        disabled={!ok}
                        aria-pressed={selected}
                        aria-label={`${g.name} ${r.region} ${c ? STOCK_LABEL[c.stock] : '없음'}`}
                        onClick={() => setPicked({ gpuType: g.gpu_type, region: r.region })}
                        className={cn(
                          'flex w-full min-w-[84px] flex-col items-start rounded-md px-2 py-1.5 text-left ring-1 ring-border',
                          selected && 'bg-point-soft ring-2 ring-primary',
                          !ok && 'cursor-not-allowed opacity-40',
                        )}
                      >
                        <span>{c ? STOCK_LABEL[c.stock] : '없음'}</span>
                        <span className="font-mono text-xs text-muted-foreground">
                          {c?.price_per_hour_usd != null
                            ? `${formatUsd(c.price_per_hour_usd)}/h`
                            : '—'}
                        </span>
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!pick && (
        <p className="text-sm">
          켤 수 있는 재고가 없습니다. 잠시 뒤 다시 보거나 GPU 설정을 바꾸세요.
        </p>
      )}
      {otherGpu && <p className="text-sm text-muted-foreground">{ENGINE_NOTE}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="secondary" onClick={onCancel}>
          취소
        </Button>
        <Button
          disabled={!pick || (cell !== undefined && !usable(cell))}
          onClick={() => setConfirming(true)}
        >
          켜기
        </Button>
      </div>
    </div>
  );
}
