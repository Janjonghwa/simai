import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { useCamSplitStore, type CamSplit } from '@/features/cctv/camSplit.store';
import { stateText, type CameraState } from '@/features/cctv/cctv';
import { useCctvStore } from '@/features/cctv/cctv.store';
import { CctvMockFeed, LiveBadge, type PersonTags } from '@/features/cctv/CctvDemo';
import { CCTV_CAMS } from '@/features/cctv/cctvMockView';
import { DIMMED } from '@/features/cctv/tileStyle';
import { useCameraStates } from '@/features/cctv/useCameraStates';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { AckButton } from '@/features/events/AckButton';
import { entryStatus, isActive } from '@/features/events/entries';
import { useEventsStore } from '@/features/events/events.store';
import { ruleName, statusText } from '@/features/events/format';
import { bestCamera, groupIncidents, topEntry, type Incident } from '@/features/events/incidents';
import { SeverityBadge } from '@/features/events/SeverityBadge';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { env } from '@/lib/env';
import { useRealtimeStore } from '@/store/realtime.store';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { Video, X } from 'lucide-react';
import { useCallback, useEffect, useMemo } from 'react';

// 실시간 관제 오른쪽 카메라 미리보기(관제 데모의 3D | CCTV 칸). 이벤트 카드·경보 배너의 「camN로 확인」이 연다.
// 카메라 탭, 영상(사람 ID 박스), 이 사람의 사건 요약과 경보 확인·사건 상세를 한 칸에 둔다. 영상은 관리자만 본다
// (명세 3.3). 다른 역할은 카메라 상태만 본다. 영상은 알림·판정에 쓰지 않는다.

/** 위험 등급이 있는 사람만 박스 색을 입힌다(진행 중 규칙 중 가장 높은 등급). */
function personTags(incidents: readonly Incident[]): PersonTags {
  const tags: Record<string, PersonTags[string]> = {};
  for (const incident of incidents) {
    if (!incident.active || incident.target?.type !== 'person') continue;
    const { severity } = incident;
    if (severity === 'CRITICAL' || severity === 'HIGH' || severity === 'CAUTION') {
      tags[incident.target.id] = severity;
    }
  }
  return tags;
}

/** 관리자: 중계가 이어져 이 카메라 영상이 오면 그 영상을, 아니면 모의 화면을 그린다. */
function AdminFeed({
  camId,
  down,
  frozen,
  tags,
  focus,
}: {
  camId: string;
  down: boolean;
  frozen: boolean;
  tags: PersonTags;
  focus: string | null;
}) {
  const attach = useCctvPreview(env.VITE_CCTV_PREVIEW_URL);
  const link = useCctvStore((s) => s.link);
  const cameras = useCctvStore((s) => s.cameras);
  const ref = useCallback(
    (canvas: HTMLCanvasElement | null) => attach(camId, canvas),
    [attach, camId],
  );
  if (link === 'open' && cameras.includes(camId)) {
    return (
      <canvas
        key={camId}
        ref={ref}
        aria-label={`${camId} 영상`}
        className={cn('size-full object-contain', (down || frozen) && DIMMED)}
      />
    );
  }
  return <CctvMockFeed camId={camId} down={down} frozen={frozen} tags={tags} focus={focus} />;
}

/** 관리자가 아닐 때: 영상 없이 카메라 상태만. */
function StateFeed({
  camId,
  state,
  lastClock,
}: {
  camId: string;
  state: CameraState;
  lastClock?: string;
}) {
  return (
    <div
      role="group"
      aria-label={`${camId} 상태`}
      className="flex size-full items-center justify-center"
    >
      <span
        className={cn('text-lg', state === 'live' ? 'font-medium' : 'font-semibold text-delay')}
      >
        {stateText(state, lastClock)}
      </span>
    </div>
  );
}

function IncidentSummary({
  incident,
  onClose,
}: {
  incident: Incident | undefined;
  onClose: () => void;
}) {
  const openEvent = useEventsStore((s) => s.openEvent);
  if (!incident) {
    return <p className="text-sm text-muted-foreground">활성 사건이 없습니다</p>;
  }
  const top = topEntry(incident);
  const status = entryStatus(top);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex min-w-0 items-center gap-2">
        {incident.target && <b className="font-mono">{incident.target.id}</b>}
        <SeverityBadge severity={top.event.severity} />
        <span className="ml-auto text-xs text-muted-foreground">{statusText(status)}</span>
      </div>
      <p className="text-sm">
        {ruleName(top.event.rule_id)}
        {top.event.zone_id && <> · {top.event.zone_id}</>}
      </p>
      <div className="flex items-center justify-end gap-2">
        <AckButton entry={top} label="경보 확인(ACK)" />
        <Button
          size="sm"
          variant="secondary"
          onClick={() => {
            openEvent(top.event.event_id);
            onClose();
          }}
        >
          사건 상세
        </Button>
      </div>
    </div>
  );
}

function Panel({ split }: { split: CamSplit }) {
  const setCam = useCamSplitStore((s) => s.setCam);
  const close = useCamSplitStore((s) => s.close);
  const isAdmin = useCurrentRole() === 'ADMIN';
  const { entries } = useLiveEvents();
  const { fresh, lastClock, stateOf } = useCameraStates();
  const best = useRealtimeStore((s) => bestCamera(s.world?.payload.tracks, split.target));

  const incidents = useMemo(() => groupIncidents(entries), [entries]);
  const { target } = split;
  const incident = target
    ? incidents.find((i) => i.target?.type === target.type && i.target.id === target.id)
    : undefined;
  const tags = useMemo(() => personTags(incidents), [incidents]);

  const cam = CCTV_CAMS.find((c) => c.id === split.cam) ?? CCTV_CAMS[0]!;
  const state = stateOf(cam.id);
  const down = state === 'down';
  const focus = split.target?.type === 'person' ? split.target.id : null;

  // Esc 로 닫는다(입력 칸에 있을 때는 제외).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if ((e.target as HTMLElement | null)?.closest?.('input, textarea, select')) return;
      close();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [close]);

  return (
    <section
      aria-label="CCTV 확인"
      className="absolute top-[72px] right-3.5 bottom-3.5 z-[6] flex w-[clamp(340px,34%,560px)] flex-col gap-2.5 overflow-y-auto rounded-[16px] bg-card/94 p-3 shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset motion-safe:animate-in motion-safe:duration-200 motion-safe:fade-in motion-safe:slide-in-from-right-8"
    >
      <div className="flex items-center gap-2">
        <Video aria-hidden className="size-[18px]" />
        <b>
          <span className="font-mono">{cam.id}</span> {cam.dir} 모서리
        </b>
        <span className="ml-auto inline-flex items-center gap-1.5 rounded-md bg-surface-2 px-2 py-0.5 font-mono text-xs">
          {down ? '끊김' : fresh ? <LiveBadge frozen={false} /> : <LiveBadge frozen />}
        </span>
        <button
          type="button"
          aria-label="CCTV 닫기 (Esc)"
          title="닫기 (Esc)"
          onClick={close}
          className="inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-surface-2 hover:text-foreground"
        >
          <X aria-hidden className="size-4" />
        </button>
      </div>
      <div
        role="tablist"
        aria-label="카메라"
        className="grid grid-cols-4 gap-1 rounded-lg bg-surface-2 p-1"
      >
        {CCTV_CAMS.map((c) => {
          const dn = stateOf(c.id) === 'down';
          const selected = c.id === cam.id;
          return (
            <button
              key={c.id}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => setCam(c.id)}
              className={cn(
                'inline-flex items-center justify-center gap-1.5 rounded-md py-1.5 text-sm',
                selected
                  ? 'bg-card font-semibold ring-1 ring-primary ring-inset'
                  : 'hover:bg-surface-3',
                dn && 'text-delay',
              )}
            >
              <span className="font-mono">{c.id}</span>
              {dn ? (
                <em className="text-xs not-italic">끊김</em>
              ) : c.id === best ? (
                <em className="rounded bg-primary/15 px-1 text-xs font-semibold text-primary not-italic">
                  추천
                </em>
              ) : null}
            </button>
          );
        })}
      </div>
      <div className="aspect-video w-full overflow-hidden rounded-lg bg-slate-900">
        {isAdmin ? (
          <AdminFeed
            key={cam.id}
            camId={cam.id}
            down={down}
            frozen={!fresh}
            tags={tags}
            focus={focus}
          />
        ) : (
          <StateFeed camId={cam.id} state={state} lastClock={lastClock} />
        )}
      </div>
      {down && best && best !== cam.id && (
        <Button size="sm" onClick={() => setCam(best)}>
          {best}로 보기
        </Button>
      )}
      <div className="rounded-[10px] bg-surface-2 p-3">
        <IncidentSummary
          incident={
            incident && incident.entries.some((e) => isActive(e.event)) ? incident : undefined
          }
          onClose={close}
        />
      </div>
    </section>
  );
}

/** 열려 있을 때만 그린다. 실시간 관제(이벤트 패널이 있는 화면)에서만 둔다. */
export function CamSplitPanel() {
  const split = useCamSplitStore((s) => s.split);
  return split ? <Panel split={split} /> : null;
}
