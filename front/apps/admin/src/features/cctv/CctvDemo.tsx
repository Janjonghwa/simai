import {
  CCTV_CAMS,
  CCTV_H,
  CCTV_W,
  cctvBackdrop,
  cctvFigures,
  cctvProj,
  type CctvCam,
  type CctvProjector,
} from '@/features/cctv/cctvMockView';
import { clockLabel, lastDataClock } from '@/features/layout/status';
import { useNow } from '@/features/layout/useNow';
import { toPlan, trackLook } from '@/features/plan/plan';
import { useWorldFresh } from '@/features/plan/useWorldFresh';
import { cameraRows } from '@/features/system/health';
import { useLinkAlive } from '@/hooks/useLinkAlive';
import { useRealtimeStore } from '@/store/realtime.store';
import type { WorldTrack } from '@simai/contracts';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useState } from 'react';

// 중계와 이어지지 않았을 때의 CCTV 모의 화면(관제 데모와 같다). 실제 영상 대신 각 카메라에서 본 교실에
// World 의 사람을 투영해 그린다. 그림은 두 모드 모두 어두운 영상처럼 보이게 slate 색만 쓴다.

const NO_TRACKS: WorldTrack[] = [];

interface CamView {
  cam: CctvCam;
  project: CctvProjector;
  grid: string;
  room: string;
}

// 바닥 격자·방 테두리는 바뀌지 않으므로 한 번만 계산한다.
const VIEWS: readonly CamView[] = CCTV_CAMS.map((cam) => {
  const project = cctvProj(cam);
  return { cam, project, ...cctvBackdrop(project) };
});

const PILL =
  'pointer-events-none absolute top-2 rounded-md bg-slate-950/70 px-2 py-0.5 text-xs leading-[18px] text-slate-100';

/** World 의 사람. world 가 올 때마다(10Hz) 이 층만 다시 그린다. */
function People({ project }: { project: CctvProjector }) {
  const tracks = useRealtimeStore((s) => s.world?.payload.tracks) ?? NO_TRACKS;
  const people = tracks
    .filter((track) => trackLook(track) !== 'unknown')
    .map((track) => {
      const [x, y] = toPlan(...track.position_xy);
      return { id: track.world_id, x, y };
    });
  return cctvFigures(project, people).map(({ id, body, head }) => (
    <g key={id}>
      <rect {...body} className="fill-slate-400" />
      <circle {...head} className="fill-slate-300" />
    </g>
  ));
}

function Feed({ view, down, frozen }: { view: CamView; down: boolean; frozen: boolean }) {
  const sky = `cctv-sky-${view.cam.id}`;
  return (
    <svg
      viewBox={`0 0 ${CCTV_W} ${CCTV_H}`}
      preserveAspectRatio="xMidYMid slice"
      role="img"
      aria-label={`${view.cam.id} 카메라 영상`}
      className={cn('block size-full', frozen && 'opacity-45')}
    >
      <defs>
        <linearGradient id={sky} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="currentColor" className="text-slate-800" />
          <stop offset="1" stopColor="currentColor" className="text-slate-700" />
        </linearGradient>
      </defs>
      <rect width={CCTV_W} height={CCTV_H} fill={`url(#${sky})`} />
      {down ? (
        <g>
          <rect width={CCTV_W} height={CCTV_H} className="fill-slate-900" />
          <text
            x={CCTV_W / 2}
            y={CCTV_H / 2}
            textAnchor="middle"
            dominantBaseline="middle"
            fontSize={7}
            className="fill-slate-400"
          >
            신호 없음
          </text>
        </g>
      ) : (
        <g>
          <path d={view.grid} strokeWidth={0.3} className="fill-none stroke-slate-600" />
          <path d={view.room} strokeWidth={0.6} className="fill-none stroke-slate-400" />
          <People project={view.project} />
        </g>
      )}
    </svg>
  );
}

/** EC2 시계. 1초마다 바뀌므로 이 글자만 다시 그린다. */
function LiveClock() {
  const now = useNow();
  const offset = useRealtimeStore((s) => s.health?.clockOffsetMs);
  return <>LIVE {clockLabel(offset, now) ?? '—'}</>;
}

function Badge({ down, frozen }: { down: boolean; frozen: boolean }) {
  const last = useRealtimeStore((s) => lastDataClock(s.health));
  return (
    <span
      className={cn(
        PILL,
        'right-2 inline-flex items-center gap-1.5 font-mono',
        (down || frozen) && 'text-slate-400',
      )}
    >
      {down ? (
        '끊김'
      ) : frozen ? (
        `정지 · ${last ?? '—'}`
      ) : (
        <>
          <i aria-hidden className="size-[7px] rounded-full bg-sev-critical" />
          <LiveClock />
        </>
      )}
    </span>
  );
}

function Tile({
  view,
  focused,
  down,
  frozen,
  onToggle,
  className,
}: {
  view: CamView;
  focused: boolean;
  down: boolean;
  frozen: boolean;
  onToggle: () => void;
  className?: string;
}) {
  const { id, dir } = view.cam;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={`${id} ${focused ? '작게 보기' : '크게 보기'}`}
      // 테두리는 그림 위에 겹쳐 그린다(안쪽 그림자는 그림에 가린다).
      className={cn(
        'relative flex-none cursor-zoom-in overflow-hidden rounded-[10px] bg-slate-900 outline-none after:pointer-events-none after:absolute after:inset-0 after:rounded-[inherit] after:ring-1 after:ring-border after:ring-inset focus-visible:ring-3 focus-visible:ring-ring/50',
        focused && 'cursor-zoom-out after:ring-2 after:ring-primary',
        className,
      )}
    >
      <Feed view={view} down={down} frozen={frozen} />
      <span className={cn(PILL, 'left-2')}>
        <b className="font-mono">{id}</b> {dir} 모서리
      </span>
      <Badge down={down} frozen={frozen} />
    </button>
  );
}

/** 2×2 화면. 한 칸을 누르면 그 칸을 크게, 나머지 셋을 옆에 세로로 둔다. */
export function CctvDemo() {
  const [focus, setFocus] = useState<string | null>(null);
  const alive = useLinkAlive();
  const health = useRealtimeStore((s) => (alive ? s.health : null));
  const fresh = useWorldFresh();
  const down = new Set(
    cameraRows(health)
      .filter((row) => !row.ok)
      .map((row) => row.id),
  );

  const tile = (view: CamView, className?: string) => (
    <Tile
      key={view.cam.id}
      view={view}
      focused={focus === view.cam.id}
      down={down.has(view.cam.id)}
      frozen={!fresh}
      onToggle={() => setFocus(focus === view.cam.id ? null : view.cam.id)}
      className={className}
    />
  );
  const focused = VIEWS.find((view) => view.cam.id === focus);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {focused && (
        <div className="flex justify-end">
          <Button variant="outline" size="sm" onClick={() => setFocus(null)}>
            4분할로
          </Button>
        </div>
      )}
      {/* 남은 공간에 맞는 16:9 상자를 컨테이너 단위로 계산한다. */}
      <div className="[container-type:size] flex min-h-0 flex-1 items-start justify-center">
        {focused ? (
          <div className="flex h-(--h) gap-2.5 [--h:min(100cqh,calc((100cqw_-_10px)*27/64))]">
            {tile(focused, 'h-(--h) w-[calc(var(--h)*16/9)]')}
            <div className="flex flex-col gap-2.5">
              {VIEWS.filter((view) => view !== focused).map((view) =>
                tile(view, 'h-[calc((var(--h)_-_20px)/3)] w-[calc((var(--h)_-_20px)/3*16/9)]'),
              )}
            </div>
          </div>
        ) : (
          <div className="grid h-(--g) w-[calc(var(--g)*16/9)] grid-cols-2 grid-rows-2 gap-2.5 [--g:min(100cqh,calc(100cqw*9/16))]">
            {VIEWS.map((view) => tile(view, 'size-full'))}
          </div>
        )}
      </div>
    </div>
  );
}
