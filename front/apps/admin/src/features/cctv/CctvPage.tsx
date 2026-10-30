import { useCurrentRole } from '@/features/auth/useCurrentRole';
import {
  framesArriving,
  isStale,
  STATE_LABEL,
  stateText,
  tileLabel,
  type CameraState,
} from '@/features/cctv/cctv';
import { useCctvStore, type PreviewLink } from '@/features/cctv/cctv.store';
import { CctvDemo } from '@/features/cctv/CctvDemo';
import { CctvWall, type WallTile } from '@/features/cctv/CctvWall';
import { frameAgeSec } from '@/features/cctv/protocol';
import { DIMMED, LIVE_DOT, TILE_FRAME, tileZoom } from '@/features/cctv/tileStyle';
import { useCameraStates } from '@/features/cctv/useCameraStates';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { PageTitle } from '@/features/layout/PageTitle';
import { useNow } from '@/features/layout/useNow';
import { env } from '@/lib/env';
import { cn } from '@simai/ui/lib/utils';
import { useCallback } from 'react';

// CCTV 관제(명세 3.3·16장, FR-133): 현장 노트북 영상 중계가 이미 받은 카메라 4대 H.264 를 루프백 WebSocket 으로 받아
// WebCodecs 로 원본 해상도 그대로 4분할에 그린다. 한 칸을 누르면 그 칸을 크게, 나머지를 옆에 둔다. 영상은 EC2 를 거치지
// 않는다. 영상은 알림·판정에 쓰지 않는다. 영상은 관리자만 본다. 다른 역할은 카메라 상태(실시간·끊김·정지)만 본다.
// 관리자인데 중계와 이어지지 않은 동안(연결 중·붙지 못함·끊김·WebCodecs 없음)은 연결 상태 한 줄과 그 아래 모의 화면을 보인다.

type Attach = (cameraId: string, canvas: HTMLCanvasElement | null) => void;

/** 프레임 나이를 다시 그리는 간격(ms). 지연 기준(STALE_SEC 1초)보다 짧게 잡는다. */
const FRAME_AGE_TICK_MS = 250;

const CAPTION =
  'absolute flex items-center gap-2 rounded-md bg-card/80 px-2 py-1 text-sm backdrop-blur-[4px]';

/** 마지막 프레임이 몇 초 전인지. 이 훅을 쓰는 칸만 다시 그린다. */
function useFrameAge(cameraId: string): number | undefined {
  const now = useNow(FRAME_AGE_TICK_MS);
  const last = useCctvStore((s) => s.views[cameraId]?.lastCaptureMs);
  return frameAgeSec(last, now);
}

function FrameAge({ cameraId }: { cameraId: string }) {
  const age = useFrameAge(cameraId);
  if (age === undefined) return null;
  return (
    <span className={cn('font-mono tabular-nums', isStale(age) && 'text-delay')}>
      {age.toFixed(1)}초
    </span>
  );
}

/** 칸 오른쪽 위 상태. LIVE 점은 프레임이 실제로 들어올 때만 켠다. */
function StateBadge({
  cameraId,
  state,
  lastClock,
}: {
  cameraId: string;
  state: CameraState;
  lastClock?: string;
}) {
  const age = useFrameAge(cameraId);
  const phase = useCctvStore((s) => s.views[cameraId]?.phase);
  const link = useCctvStore((s) => s.link);
  const live = state === 'live' && framesArriving(phase, link, age);
  return (
    <span
      className={cn(CAPTION, 'top-2 right-2', state !== 'live' && 'font-semibold')}
      aria-label={`${cameraId} 상태 ${STATE_LABEL[state]}`}
    >
      {live ? (
        <>
          <i aria-hidden className={LIVE_DOT} />
          <span className="font-mono">LIVE</span>
        </>
      ) : (
        stateText(state, lastClock)
      )}
    </span>
  );
}

function CameraTile({
  cameraId,
  attach,
  state,
  lastClock,
  wall,
}: {
  cameraId: string;
  attach: Attach;
  state: CameraState;
  lastClock?: string;
  wall: WallTile;
}) {
  const phase = useCctvStore((s) => s.views[cameraId]?.phase);
  const link = useCctvStore((s) => s.link);
  const playing = link === 'open' && phase === 'playing';
  const ref = useCallback(
    (canvas: HTMLCanvasElement | null) => attach(cameraId, canvas),
    [attach, cameraId],
  );
  return (
    <button
      type="button"
      onClick={wall.toggle}
      aria-label={`${cameraId} ${wall.focused ? '작게 보기' : '크게 보기'}`}
      className={cn(TILE_FRAME, 'bg-surface-2', tileZoom(wall.focused))}
    >
      <canvas
        ref={ref}
        aria-label={`${cameraId} 영상`}
        className={cn('size-full object-contain', state !== 'live' && DIMMED)}
      />
      <span className={cn(CAPTION, 'top-2 left-2')}>
        <span className="font-mono font-semibold">{cameraId}</span>
        <span className={cn(!playing && 'font-semibold')}>{tileLabel(phase, link)}</span>
        <FrameAge cameraId={cameraId} />
      </span>
      <StateBadge cameraId={cameraId} state={state} lastClock={lastClock} />
    </button>
  );
}

/** 관리자가 아닐 때: 영상 없이 카메라 상태만 보인다. */
function StateTile({
  cameraId,
  state,
  lastClock,
}: {
  cameraId: string;
  state: CameraState;
  lastClock?: string;
}) {
  return (
    <div
      role="group"
      aria-label={`${cameraId} 상태`}
      className={cn(TILE_FRAME, 'flex items-center justify-center bg-surface-2')}
    >
      <span className={cn(CAPTION, 'top-2 left-2 font-mono font-semibold')}>{cameraId}</span>
      <span
        className={cn('text-xl', state === 'live' ? 'font-medium' : 'font-semibold text-delay')}
      >
        {stateText(state, lastClock)}
      </span>
    </div>
  );
}

/** 영상 없이 카메라 상태만(관리자가 아닌 역할). 카메라 목록은 Tier 0 health 에서 온다. */
function StatesOnly() {
  const { cameras, lastClock, stateOf } = useCameraStates();
  if (cameras.length === 0) {
    return (
      <p role="status" className="text-muted-foreground">
        카메라 상태를 받지 못했습니다
      </p>
    );
  }
  return (
    <CctvWall
      ids={cameras.map((camera) => camera.id)}
      focusable={false}
      tile={(id) => <StateTile cameraId={id} state={stateOf(id)} lastClock={lastClock} />}
    />
  );
}

/** 중계와 이어지지 않았을 때: 연결 상태 한 줄과 그 아래 모의 화면(한 번 붙은 뒤 끊겨도 같다). */
function MockWithStatus({ link }: { link: PreviewLink }) {
  return (
    <>
      <p role="status" className="text-sm font-semibold">
        {tileLabel(undefined, link)}
      </p>
      <CctvDemo />
    </>
  );
}

function AdminCctv() {
  const attach = useCctvPreview(env.VITE_CCTV_PREVIEW_URL);
  const link = useCctvStore((s) => s.link);
  const cameras = useCctvStore((s) => s.cameras);
  const { lastClock, stateOf } = useCameraStates();

  if (link !== 'open') return <MockWithStatus link={link} />;
  if (cameras.length === 0)
    return <p className="text-muted-foreground">{tileLabel(undefined, link)}</p>;
  return (
    <CctvWall
      ids={cameras}
      tile={(id, wall) => (
        <CameraTile
          cameraId={id}
          attach={attach}
          state={stateOf(id)}
          lastClock={lastClock}
          wall={wall}
        />
      )}
    />
  );
}

export function CctvPage() {
  const isAdmin = useCurrentRole() === 'ADMIN';
  return (
    <>
      <PageTitle>CCTV 관제</PageTitle>
      {isAdmin ? <AdminCctv /> : <StatesOnly />}
    </>
  );
}
