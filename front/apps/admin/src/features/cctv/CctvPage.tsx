import { isStale, noticeText, showsNotice, tileLabel } from '@/features/cctv/cctv';
import { useCctvStore } from '@/features/cctv/cctv.store';
import { frameAgeSec } from '@/features/cctv/protocol';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { PageTitle } from '@/features/layout/PageTitle';
import { useNow } from '@/features/layout/useNow';
import { cameraRows } from '@/features/system/health';
import { useLinkAlive } from '@/hooks/useLinkAlive';
import { env } from '@/lib/env';
import { useRealtimeStore } from '@/store/realtime.store';
import { Alert, AlertDescription, AlertTitle } from '@simai/ui/components/alert';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';

// CCTV 관제(명세 3.3·16장, FR-133): 현장 노트북 영상 중계가 이미 받은 카메라 4대 H.264 를 루프백 WebSocket 으로 받아
// WebCodecs 로 원본 해상도 그대로 4분할에 그린다. 영상은 EC2 를 거치지 않으므로 현장 노트북이 아니면 안내와
// EC2 가 알린 카메라 상태만 보인다. 영상은 알림·판정에 쓰지 않는다.

type Attach = (cameraId: string, canvas: HTMLCanvasElement | null) => void;

function FrameAge({ cameraId }: { cameraId: string }) {
  const now = useNow(250);
  const last = useCctvStore((s) => s.views[cameraId]?.lastCaptureMs);
  const age = frameAgeSec(last, now);
  if (age === undefined) return null;
  return (
    <span className={cn('font-mono tabular-nums', isStale(age) && 'text-delay')}>
      {age.toFixed(1)}초
    </span>
  );
}

function CameraTile({ cameraId, attach }: { cameraId: string; attach: Attach }) {
  const phase = useCctvStore((s) => s.views[cameraId]?.phase);
  const link = useCctvStore((s) => s.link);
  const playing = link === 'open' && phase === 'playing';
  return (
    <figure className="relative aspect-video overflow-hidden rounded-lg bg-surface-2 ring-1 ring-border">
      <canvas
        ref={(canvas) => attach(cameraId, canvas)}
        aria-label={`${cameraId} 영상`}
        className="size-full object-contain"
      />
      <figcaption className="absolute top-2 left-2 flex items-center gap-2 rounded-md bg-card/80 px-2 py-1 text-sm backdrop-blur-[4px]">
        <span className="font-mono font-semibold">{cameraId}</span>
        <span className={cn(!playing && 'font-semibold')}>{tileLabel(phase, link)}</span>
        <FrameAge cameraId={cameraId} />
      </figcaption>
    </figure>
  );
}

/** 현장 노트북이 아닐 때: EC2 가 알린 카메라 상태(시스템 상태 화면과 같은 값). */
function CameraStatus() {
  const alive = useLinkAlive();
  const rows = cameraRows(useRealtimeStore((s) => (alive ? s.health : null)));
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>카메라 상태</CardTitle>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="text-muted-foreground">카메라 상태 없음</p>
        ) : (
          <ul aria-label="카메라 상태" className="grid grid-cols-4 gap-3">
            {rows.map((row) => (
              <li key={row.id} className="flex items-center gap-2">
                <span className="font-mono">{row.id}</span>
                <span className={cn(!row.ok && 'font-semibold')}>{row.status ?? '—'}</span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

export function CctvPage() {
  const attach = useCctvPreview(env.VITE_CCTV_PREVIEW_URL);
  const link = useCctvStore((s) => s.link);
  const everOpened = useCctvStore((s) => s.everOpened);
  const cameras = useCctvStore((s) => s.cameras);

  if (showsNotice(link, everOpened)) {
    return (
      <>
        <PageTitle>CCTV 관제</PageTitle>
        <Alert>
          <AlertTitle>현장 노트북에서만 표시</AlertTitle>
          <AlertDescription>{noticeText(link)}</AlertDescription>
        </Alert>
        <CameraStatus />
      </>
    );
  }

  return (
    <>
      <PageTitle>CCTV 관제</PageTitle>
      {cameras.length === 0 ? (
        <p className="text-muted-foreground">{tileLabel(undefined, link)}</p>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          {cameras.map((id) => (
            <CameraTile key={id} cameraId={id} attach={attach} />
          ))}
        </div>
      )}
    </>
  );
}
