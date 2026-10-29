import { isStale, tileLabel } from '@/features/cctv/cctv';
import { useCctvStore } from '@/features/cctv/cctv.store';
import { CctvDemo } from '@/features/cctv/CctvDemo';
import { frameAgeSec } from '@/features/cctv/protocol';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { PageTitle } from '@/features/layout/PageTitle';
import { useNow } from '@/features/layout/useNow';
import { env } from '@/lib/env';
import { cn } from '@simai/ui/lib/utils';

// CCTV 관제(명세 3.3·16장, FR-133): 현장 노트북 영상 중계가 이미 받은 카메라 4대 H.264 를 루프백 WebSocket 으로 받아
// WebCodecs 로 원본 해상도 그대로 4분할에 그린다. 영상은 EC2 를 거치지 않는다. 영상은 알림·판정에 쓰지 않는다.
// 중계와 이어지지 않은 동안(연결 중·붙지 못함·끊김·WebCodecs 없음)은 연결 상태 한 줄과 그 아래 모의 화면을 보인다.

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

export function CctvPage() {
  const attach = useCctvPreview(env.VITE_CCTV_PREVIEW_URL);
  const link = useCctvStore((s) => s.link);
  const cameras = useCctvStore((s) => s.cameras);

  // 현장 노트북 중계와 이어지지 않았으면 모의 화면을 보인다(한 번 붙은 뒤 끊겨도 같다).
  if (link !== 'open') {
    return (
      <>
        <PageTitle>CCTV 관제</PageTitle>
        <p role="status" className="text-sm font-semibold">
          {tileLabel(undefined, link)}
        </p>
        <CctvDemo />
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
