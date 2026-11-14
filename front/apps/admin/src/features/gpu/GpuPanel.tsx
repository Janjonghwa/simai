import { GpuInfoLines } from '@/features/gpu/GpuInfoLines';
import { GpuLaunchForm } from '@/features/gpu/GpuLaunchForm';
import { gpuErrorMessage, isGpuOn, STOP_BLOCK_TEXT } from '@/features/gpu/gpuStatus';
import { useGpuStatus, useGpuStop } from '@/features/gpu/useGpu';
import { ModalDialog } from '@/features/tbm/ModalDialog';
import { Button } from '@simai/ui/components/button';
import { useState } from 'react';

// GPU 켜기·끄기 패널(S15P31A101-702). 상단바 GPU 점을 누르면 열린다.
// 꺼져 있으면 재고 표(GpuLaunchForm), 켜져 있거나 켜는 중이면 요약과 「GPU 끄기」. 관제(LIVE)·세팅 중이면 끄기를 막고 까닭을 보인다.
// 끄기는 확인을 한 번 더 받는다.

export function GpuPanel({ onClose }: { onClose: () => void }) {
  const { status } = useGpuStatus();
  const on = isGpuOn(status);
  return (
    <ModalDialog label="GPU" onClose={onClose} className="w-[min(640px,calc(100vw-32px))]">
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <h2 className="text-base font-semibold">{on ? 'GPU' : 'GPU 켜기'}</h2>
      </div>
      {on ? (
        <GpuStopView onClose={onClose} />
      ) : (
        <GpuLaunchForm onLaunched={onClose} onCancel={onClose} />
      )}
    </ModalDialog>
  );
}

function GpuStopView({ onClose }: { onClose: () => void }) {
  const { status } = useGpuStatus();
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
          <p className="text-sm font-semibold">GPU 를 끌까요? 다시 켜면 준비에 몇 분 걸립니다.</p>
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
