import { useCurrentRole } from '@/features/auth/useCurrentRole';
import {
  framesArriving,
  isStale,
  remoteNotice,
  STATE_LABEL,
  stateText,
  tileLabel,
  usageText,
  type CameraState,
} from '@/features/cctv/cctv';
import { useCctvStore, type PreviewLink } from '@/features/cctv/cctv.store';
import { CctvDemo } from '@/features/cctv/CctvDemo';
import { CctvWall, type WallTile } from '@/features/cctv/CctvWall';
import { isZoomed, panZoomTransform, visibleRect, type PanZoomView } from '@/features/cctv/panZoom';
import { frameAgeSec } from '@/features/cctv/protocol';
import { DIMMED, LIVE_DOT, TILE_FRAME, tilePan, tileZoom } from '@/features/cctv/tileStyle';
import { useCameraStates } from '@/features/cctv/useCameraStates';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { usePanZoom } from '@/features/cctv/usePanZoom';
import { useRemoteStatus } from '@/features/cctv/useRemoteStatus';
import { PageTitle } from '@/features/layout/PageTitle';
import { useNow } from '@/features/layout/useNow';
import { env } from '@/lib/env';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useCallback } from 'react';

// CCTV 관제(명세 3.3·16장, FR-133): 현장 노트북 영상 중계가 이미 받은 카메라 4대 H.264 를 WebCodecs 로 원본 해상도 그대로
// 4분할에 그린다. 한 칸을 누르면 그 칸을 크게, 나머지를 옆에 둔다. 같은 노트북이면 루프백(127.0.0.1)으로, 아니면 EC2 원격 시청
// (S15P31A101-400, 저장 없이 나눠 줌, 보는 사람이 있을 때만 게이트웨이가 올림)으로 받는다. 영상은 알림·판정에 쓰지 않는다.
// 영상은 관리자만 본다. 다른 역할은 카메라 상태(실시간·끊김·정지)만 본다.
// 관리자인데 이어지지 않은 동안(연결 중·붙지 못함·끊김·WebCodecs 없음·원격 멈춤)은 상태 한 줄과 그 아래 모의 화면을 보인다.
// 영상 칸은 휠로 확대하고 확대 중에는 끌어서 옮긴다(usePanZoom). 캔버스에 CSS transform 만 주므로 그리기는 그대로다.

type Attach = (cameraId: string, canvas: HTMLCanvasElement | null) => void;

/** 프레임 나이를 다시 그리는 간격(ms). 지연 기준(STALE_SEC 1초)보다 짧게 잡는다. */
const FRAME_AGE_TICK_MS = 250;

const CAPTION =
  'absolute flex items-center gap-2 rounded-md bg-card/80 px-2 py-1 text-sm backdrop-blur-[4px]';

/** 칸의 마지막 프레임 시각(field)이 몇 초 전인지. 이 훅을 쓰는 칸만 다시 그린다. */
function useSecondsSince(
  cameraId: string,
  field: 'lastCaptureMs' | 'lastDecodedMs',
): number | undefined {
  const now = useNow(FRAME_AGE_TICK_MS);
  const at = useCctvStore((s) => s.views[cameraId]?.[field]);
  return frameAgeSec(at, now);
}

function FrameAge({ cameraId }: { cameraId: string }) {
  const age = useSecondsSince(cameraId, 'lastCaptureMs');
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
  const sinceFrameSec = useSecondsSince(cameraId, 'lastDecodedMs');
  const phase = useCctvStore((s) => s.views[cameraId]?.phase);
  const link = useCctvStore((s) => s.link);
  const live = state === 'live' && framesArriving(phase, link, sinceFrameSec);
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

const toPercent = (ratio: number) => `${ratio * 100}%`;

/** 확대 중 오른쪽 아래에 지금 보는 자리를 작은 상자로 보인다. */
function ZoomMap({ view }: { view: PanZoomView }) {
  if (!isZoomed(view)) return null;
  const { left, top, width, height } = visibleRect(view);
  return (
    <span
      role="img"
      aria-label="확대한 자리"
      className="pointer-events-none absolute right-2 bottom-2 aspect-video w-24 rounded-sm bg-card/80 ring-1 ring-border backdrop-blur-[4px]"
    >
      <i
        className="absolute bg-primary/35 ring-1 ring-primary"
        style={{
          left: toPercent(left),
          top: toPercent(top),
          width: toPercent(width),
          height: toPercent(height),
        }}
      />
    </span>
  );
}

/** 카메라 영상 칸. 이 화면의 4분할과 실시간 관제의 「cam 으로 확인」 패널이 함께 쓴다. */
export function CameraTile({
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
  /** 크게 보기 배치(CctvWall)에서만. 없으면 누르는 칸이 아니라 영상만 보인다. */
  wall?: WallTile;
}) {
  const phase = useCctvStore((s) => s.views[cameraId]?.phase);
  const link = useCctvStore((s) => s.link);
  const playing = link === 'open' && phase === 'playing';
  const { ref: tileRef, view, zoomed, dragging, handlers } = usePanZoom();
  const ref = useCallback(
    (canvas: HTMLCanvasElement | null) => attach(cameraId, canvas),
    [attach, cameraId],
  );
  const content = (
    <>
      <canvas
        ref={ref}
        aria-label={`${cameraId} 영상`}
        // 흐리게는 영상 자체가 멈췄을 때만. Tier 0 상태(정지·끊김)는 오른쪽 위 배지로 알린다 —
        // Pod 가 없을 때 들어오는 영상까지 45% 로 흐려 물빠진 색으로 보였다(2026-11-02)
        className={cn(
          'size-full origin-top-left object-contain',
          zoomed && 'will-change-transform',
          !playing && DIMMED,
        )}
        style={{ transform: panZoomTransform(view) }}
      />
      <span className={cn(CAPTION, 'top-2 left-2')}>
        <span className="font-mono font-semibold">{cameraId}</span>
        <span className={cn(!playing && 'font-semibold')}>{tileLabel(phase, link)}</span>
        <FrameAge cameraId={cameraId} />
      </span>
      <StateBadge cameraId={cameraId} state={state} lastClock={lastClock} />
      <ZoomMap view={view} />
    </>
  );
  const pan = zoomed && tilePan(dragging);
  if (!wall) {
    return (
      <div ref={tileRef} {...handlers} className={cn(TILE_FRAME, 'bg-surface-2', pan)}>
        {content}
      </div>
    );
  }
  return (
    <button
      ref={tileRef}
      {...handlers}
      type="button"
      onClick={wall.toggle}
      aria-label={`${cameraId} ${wall.focused ? '작게 보기' : '크게 보기'}`}
      className={cn(TILE_FRAME, 'bg-surface-2', tileZoom(wall.focused), pan)}
    >
      {content}
    </button>
  );
}

/** 관리자가 아닐 때: 영상 없이 카메라 상태만 보인다. */
export function StateTile({
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

/** 원격 시청 안내(멈춤·상한·게이트웨이 대기)와 "다시 보기". */
function RemoteNoticeLine({ resume }: { resume: () => void }) {
  const mode = useCctvStore((s) => s.mode);
  const pause = useCctvStore((s) => s.pause);
  const remote = useCctvStore((s) => s.remote);
  const notice = remoteNotice(mode, pause, remote);
  if (!notice) return null;
  return (
    <div role="status" aria-label="원격 시청 안내" className="flex items-center gap-3 text-sm">
      <span className="font-semibold">{notice.text}</span>
      {notice.resumable && (
        <Button size="sm" variant="outline" onClick={resume}>
          다시 보기
        </Button>
      )}
    </div>
  );
}

/** 원격 CCTV 월 송출 x / 80 GB · 차단 여부(운영 상태 표시, 위험 알림 아님). 못 받으면 비운다. */
function RemoteUsage() {
  const status = useRemoteStatus(true);
  if (!status) return null;
  return (
    <p aria-label="원격 CCTV 월 송출" className="text-xs text-muted-foreground tabular-nums">
      {usageText(status.monthly_sent_bytes, status.monthly_quota_bytes, status.blocked)}
    </p>
  );
}

function AdminCctv() {
  const { attach, resume } = useCctvPreview(env.VITE_CCTV_PREVIEW_URL, env.VITE_STOMP_URL);
  return (
    <>
      <RemoteUsage />
      <RemoteNoticeLine resume={resume} />
      <AdminWall attach={attach} />
    </>
  );
}

function AdminWall({ attach }: { attach: Attach }) {
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
