import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { remoteNotice, tileLabel } from '@/features/cctv/cctv';
import { useCctvStore } from '@/features/cctv/cctv.store';
import { CameraTile, StateTile } from '@/features/cctv/CctvPage';
import { useCameraStates } from '@/features/cctv/useCameraStates';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { bestCamera } from '@/features/events/incidents';
import { targetOfKey } from '@/features/layout/spatialHost/selectionKey';
import { useFloatSpot } from '@/hooks/useFloatSpot';
import { env } from '@/lib/env';
import { FLOAT_MARGIN, type FloatSpot } from '@/lib/floatSpot';
import { useRealtimeStore } from '@/store/realtime.store';
import { useCallback, useEffect, useRef, useState } from 'react';

const KEY = 'simai.cctv.float';
type Settings = {
  spot?: FloatSpot;
  width?: number;
  height?: number;
  camera?: string;
  all?: boolean;
};
function readSettings(): Settings {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(KEY) ?? '{}');
    if (!value || typeof value !== 'object') return {};
    const v = value as Record<string, unknown>;
    const p = v.spot as Record<string, unknown> | undefined;
    return {
      spot:
        p &&
        typeof p.left === 'number' &&
        Number.isFinite(p.left) &&
        typeof p.offset === 'number' &&
        Number.isFinite(p.offset)
          ? { left: p.left, offset: p.offset }
          : undefined,
      width:
        typeof v.width === 'number' && Number.isFinite(v.width)
          ? Math.min(1000, Math.max(280, v.width))
          : undefined,
      height:
        typeof v.height === 'number' && Number.isFinite(v.height)
          ? Math.min(800, Math.max(180, v.height))
          : undefined,
      camera: typeof v.camera === 'string' ? v.camera : undefined,
      all: typeof v.all === 'boolean' ? v.all : undefined,
    };
  } catch {
    return {};
  }
}
function saveSettings(update: Settings) {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...readSettings(), ...update }));
  } catch {
    /* 저장소를 쓸 수 없어도 창은 동작한다. */
  }
}

export interface CctvCameraRequest {
  readonly cameraId: string;
  readonly seq: number;
}

export function saveCctvFloatCamera(cameraId: string) {
  saveSettings({ camera: cameraId, all: false });
}

type Attach = (cameraId: string, canvas: HTMLCanvasElement | null) => void;
function Content({
  attach,
  selectedKey,
  requestedCamera,
}: {
  attach?: Attach;
  selectedKey: string | null;
  requestedCamera?: CctvCameraRequest | null;
}) {
  const [initial] = useState(readSettings);
  const [camera, setCamera] = useState(requestedCamera?.cameraId ?? initial.camera ?? '');
  const [all, setAll] = useState(requestedCamera ? false : (initial.all ?? false));
  const link = useCctvStore((s) => s.link);
  const relayIds = useCctvStore((s) => s.cameras);
  const { cameras, stateOf, lastClock } = useCameraStates();
  const baseIds =
    attach && link === 'open' && relayIds.length ? relayIds : cameras.map((c) => c.id);
  const ids =
    requestedCamera?.cameraId && !baseIds.includes(requestedCamera.cameraId)
      ? [...baseIds, requestedCamera.cameraId]
      : baseIds;
  useEffect(() => {
    if (!requestedCamera) return;
    // 3D 카메라 마커를 클릭하면 이미 열려 있는 창도 해당 카메라 단일 보기로 전환한다.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCamera(requestedCamera.cameraId);
    setAll(false);
    saveSettings({ camera: requestedCamera.cameraId, all: false });
  }, [requestedCamera]);
  useEffect(() => {
    const target = targetOfKey(selectedKey);
    const best = bestCamera(
      useRealtimeStore.getState().world?.payload.tracks,
      target?.type === 'person' ? { type: 'person', id: target.id } : null,
    );
    if (best) {
      // 외부 3D 선택이 바뀐 순간에만 수동 카메라 선택을 갱신한다.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setCamera(best);
      setAll(false);
      saveSettings({ camera: best, all: false });
    }
  }, [selectedKey]);
  const current = ids.includes(camera) ? camera : ids[0];
  const shown = all ? ids : current ? [current] : [];
  return (
    <>
      <div className="flex flex-wrap gap-1 p-2" role="group" aria-label="카메라 선택">
        <button
          type="button"
          aria-pressed={all}
          onClick={() => {
            setAll(true);
            saveSettings({ all: true });
          }}
          className="rounded bg-surface-2 px-2 py-1"
        >
          4분할
        </button>
        {ids.map((id) => (
          <button
            type="button"
            key={id}
            aria-pressed={!all && current === id}
            onClick={() => {
              setCamera(id);
              setAll(false);
              saveSettings({ camera: id, all: false });
            }}
            className="rounded bg-surface-2 px-2 py-1"
          >
            {id}
          </button>
        ))}
      </div>
      {attach && link !== 'open' && (
        <p role="status" className="px-2 text-xs">
          {tileLabel(undefined, link)}
        </p>
      )}
      {shown.length === 0 ? (
        <p role="status" className="p-3">
          카메라 연결 정보가 없습니다
        </p>
      ) : (
        <div className={all ? 'grid min-h-0 flex-1 grid-cols-2 gap-1 p-2' : 'min-h-0 flex-1 p-2'}>
          {shown.map((id) => (
            <div key={id} className="min-h-0 min-w-0">
              {attach ? (
                <CameraTile
                  cameraId={id}
                  attach={attach}
                  state={stateOf(id)}
                  lastClock={lastClock}
                />
              ) : (
                <StateTile cameraId={id} state={stateOf(id)} lastClock={lastClock} />
              )}
            </div>
          ))}
        </div>
      )}
    </>
  );
}
function AdminContent({
  selectedKey,
  requestedCamera,
}: {
  selectedKey: string | null;
  requestedCamera?: CctvCameraRequest | null;
}) {
  const { attach, resume } = useCctvPreview(env.VITE_CCTV_PREVIEW_URL, env.VITE_STOMP_URL);
  const link = useCctvStore((s) => s.link);
  const mode = useCctvStore((s) => s.mode);
  const pause = useCctvStore((s) => s.pause);
  const remote = useCctvStore((s) => s.remote);
  const notice = remoteNotice(mode, pause, remote);
  return (
    <>
      <Content attach={attach} selectedKey={selectedKey} requestedCamera={requestedCamera} />
      {notice && <p role="status">{notice.text}</p>}
      {(notice?.resumable || link === 'stopped') && (
        <button type="button" onClick={resume}>
          다시 보기
        </button>
      )}
    </>
  );
}
export function CctvFloat({
  onClose,
  selectedKey,
  requestedCamera = null,
}: {
  onClose: () => void;
  selectedKey: string | null;
  requestedCamera?: CctvCameraRequest | null;
}) {
  const [initial] = useState(readSettings);
  const [size, setSize] = useState({ width: initial.width ?? 480, height: initial.height ?? 360 });
  const box = useRef<HTMLElement>(null);
  const saveSpot = useCallback((spot: FloatSpot) => saveSettings({ spot }), []);
  const spot = useFloatSpot(box, 'top', FLOAT_MARGIN, initial.spot ?? null, saveSpot);
  const [collapsed, setCollapsed] = useState(false);
  const isAdmin = useCurrentRole() === 'ADMIN';
  useEffect(() => {
    if (requestedCamera) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setCollapsed(false);
    }
  }, [requestedCamera]);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      if (!collapsed && el.offsetWidth && el.offsetHeight) {
        setSize((prev) =>
          prev.width === el.offsetWidth && prev.height === el.offsetHeight
            ? prev
            : { width: el.offsetWidth, height: el.offsetHeight },
        );
        saveSettings({ width: el.offsetWidth, height: el.offsetHeight });
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [collapsed]);
  return (
    <section
      ref={box}
      aria-label="떠 있는 CCTV"
      style={{
        ...spot.style,
        width: size.width,
        height: collapsed ? 'auto' : size.height,
        resize: collapsed ? 'none' : 'both',
      }}
      className="absolute top-32 left-24 z-[7] flex max-h-[calc(100%-5rem)] max-w-[calc(100%-2rem)] min-w-[280px] flex-col overflow-auto rounded-xl bg-card shadow-float ring-1 ring-border"
    >
      <div className="flex items-center gap-2 p-2">
        <div
          {...spot.handlers}
          className="flex-1 cursor-grab touch-none font-semibold select-none"
          aria-label="CCTV 창 이동"
        >
          CCTV ⋮⋮
        </div>
        <button
          type="button"
          aria-label={collapsed ? 'CCTV 펼치기' : 'CCTV 접기'}
          onClick={() => setCollapsed(!collapsed)}
        >
          {collapsed ? '펼치기' : '접기'}
        </button>
        <button type="button" aria-label="떠 있는 CCTV 닫기" onClick={onClose}>
          닫기
        </button>
      </div>
      {!collapsed &&
        (isAdmin ? (
          <AdminContent selectedKey={selectedKey} requestedCamera={requestedCamera} />
        ) : (
          <Content selectedKey={selectedKey} requestedCamera={requestedCamera} />
        ))}
    </section>
  );
}
