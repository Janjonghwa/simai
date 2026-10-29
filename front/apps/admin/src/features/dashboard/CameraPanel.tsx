import { DashPanel } from '@/features/dashboard/DashPanel';
import { cameraCount, cameraLatency } from '@/features/dashboard/dashboard';
import { DEMO_CAMERA_LATENCY_MS } from '@/features/dashboard/dashboardDemo';
import { cameraRows, type CameraRow } from '@/features/system/health';
import { useLinkAlive } from '@/hooks/useLinkAlive';
import { useRealtimeStore } from '@/store/realtime.store';
import { cn } from '@simai/ui/lib/utils';
import { Video } from 'lucide-react';

// 카메라 상태·FPS 는 health(시스템 상태 화면과 같은 값), 지연은 아직 받을 곳이 없어 데모 값이다.
// 상태 이상은 지연이 아니라 주황을 쓰지 않고 굵게만 보인다(시스템 상태 화면과 같다).

const ROW =
  'grid min-h-[42px] grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)_56px_64px] items-center gap-2 border-b border-border text-sm';
const HEAD_ROW = cn(ROW, 'min-h-8 text-xs text-muted-foreground');

/** 끊기면 지금 값을 모른다. 마지막 값을 지금 값처럼 보이지 않는다. */
function useCameraRows(): CameraRow[] {
  const alive = useLinkAlive();
  return cameraRows(useRealtimeStore((s) => (alive ? s.health : null)));
}

// 큰 창에서는 표로 읽히게 칸 역할을 준다. 카드는 버튼 하나라 역할을 두지 않는다.
function Cells({ row, table }: { row: CameraRow; table?: boolean }) {
  const role = table ? 'cell' : undefined;
  return (
    <>
      <span role={role}>
        <span
          aria-hidden
          className={cn(
            'mr-2 inline-block size-2 rounded-full align-[1px]',
            row.ok ? 'bg-ok' : 'bg-faint',
          )}
        />
        <span className="font-mono">{row.id}</span>
      </span>
      <span role={role} className={cn(!row.ok && 'font-semibold')}>
        {row.status ?? '—'}
      </span>
      <span role={role} className="text-right font-mono">
        {row.fps ?? '—'}
      </span>
      <span role={role} className="text-right font-mono">
        {cameraLatency(row, DEMO_CAMERA_LATENCY_MS) ?? '—'}
      </span>
    </>
  );
}

function HeadCells({ table }: { table?: boolean }) {
  const role = table ? 'columnheader' : undefined;
  return (
    <>
      <span role={role}>카메라</span>
      <span role={role}>상태</span>
      <span role={role} className="text-right">
        FPS
      </span>
      <span role={role} className="text-right">
        지연
      </span>
    </>
  );
}

function NoCameras() {
  return <p className="my-4 text-sm text-faint">카메라 상태 없음</p>;
}

export function CameraPanel({ onOpen }: { onOpen: () => void }) {
  const rows = useCameraRows();
  return (
    <DashPanel icon={Video} title="카메라 상태" count={cameraCount(rows)}>
      <button
        type="button"
        aria-label="카메라 전체 목록 열기"
        onClick={onOpen}
        className="flex min-h-0 flex-1 cursor-pointer flex-col overflow-hidden px-4 pt-1 pb-3 text-left"
      >
        <span className={HEAD_ROW}>
          <HeadCells />
        </span>
        {rows.length === 0 ? (
          <NoCameras />
        ) : (
          rows.map((row) => (
            <span key={row.id} className={ROW}>
              <Cells row={row} />
            </span>
          ))
        )}
      </button>
    </DashPanel>
  );
}

/** 큰 창의 카메라 표. */
export function CameraList() {
  const rows = useCameraRows();
  return (
    <div role="table" aria-label="카메라 상태" className="min-h-0 overflow-auto">
      <div role="row" className={cn(HEAD_ROW, 'px-2.5')}>
        <HeadCells table />
      </div>
      {rows.length === 0 ? (
        <NoCameras />
      ) : (
        rows.map((row) => (
          <div key={row.id} role="row" className={cn(ROW, 'px-2.5')}>
            <Cells row={row} table />
          </div>
        ))
      )}
    </div>
  );
}
