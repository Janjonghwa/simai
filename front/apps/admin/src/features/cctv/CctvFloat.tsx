import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { remoteNotice, tileLabel } from '@/features/cctv/cctv';
import { useCctvStore } from '@/features/cctv/cctv.store';
import { CameraTile, StateTile } from '@/features/cctv/CctvPage';
import { useCameraStates } from '@/features/cctv/useCameraStates';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { bestCamera, currentPersonId } from '@/features/events/incidents';
import { targetOfKey } from '@/features/layout/spatialHost/selectionKey';
import { useCameraIds } from '@/hooks/useCameraNames';
import { useFloatSpot } from '@/hooks/useFloatSpot';
import { env } from '@/lib/env';
import { FLOAT_MARGIN, type FloatSpot } from '@/lib/floatSpot';
import { useReportCctvFloatBox } from '@/store/cctvFloatBox.store';
import { useRealtimeStore } from '@/store/realtime.store';
import { Button } from '@simai/ui/components/button';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@simai/ui/components/tooltip';
import { cn } from '@simai/ui/lib/utils';
import { Maximize2, View, X } from 'lucide-react';
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
} from 'react';

// v2(-812): 끌어 옮긴 자리의 기준이 위쪽에서 아래쪽으로 바뀌어 예전(위쪽 기준) 자리는 읽지 않는다. 모두 처음엔 기본 자리에서 연다.
const KEY = 'simai.cctv.float.v2';
/**
 * 창 폭 기본값과 한계(px). 높이는 따로 두지 않고 영상 비율을 따른다(S15P31A101-798). 기본 폭은 영상 높이가 왼쪽 아래 평면
 * 지도와 같도록(--minimap-size) 보이는 카메라의 해상도 비율로 정한다(-812, 모르면 16:9 = 2304×1296). 사용자가 폭을 바꾸면
 * 그 px 를 저장해 쓴다.
 */
const defaultWidthCss = (aspect: string) => `calc(var(--minimap-size) * ${aspect})`;
const MIN_WIDTH = 280;
const MAX_WIDTH = 1000;
/** 카메라 해상도를 아직 모를 때의 영상 비율. */
const DEFAULT_ASPECT = '16 / 9';
type Settings = {
  spot?: FloatSpot;
  /**
   * 영상 폭(창 폭). 예전 `width`·`height` 는 제목 줄·안쪽 여백을 포함한 창 크기라 읽지 않고, 다음 저장 때 지운다
   * (S15P31A101-798, 높이는 이제 영상 비율이 정한다).
   */
  videoWidth?: number;
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
      videoWidth:
        typeof v.videoWidth === 'number' && Number.isFinite(v.videoWidth)
          ? Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, v.videoWidth))
          : undefined,
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

type Attach = (cameraId: string, canvas: HTMLCanvasElement | null) => void;
/** 창이 지금 보이는 카메라를 위로 알린다(오른쪽 위 「3D 에서 이 방향으로 보기」가 쓴다, -626). */
type OnCurrent = (cameraId: string | undefined) => void;

/** 영상 위에 겹쳐 보이는 상태 글. */
const STATUS = 'rounded-md bg-card/85 px-2 py-1 text-center text-sm backdrop-blur-[4px]';
/** 창은 영상만 가장자리까지 채운다. 칸의 테두리·모서리는 빼고 창의 둥근 모서리·그림자만 남긴다. */
const FRAMELESS = 'rounded-none after:ring-0';
/** 영상 위 아이콘 버튼. 밝은·어두운 영상 모두에서 보이게 반투명 바탕을 깐다. */
const OVERLAY_BUTTON = 'bg-card/80 backdrop-blur-[4px] hover:bg-card';

function Content({
  attach,
  selectedKey,
  requestedCamera,
  onCurrent,
  notice,
}: {
  attach?: Attach;
  selectedKey: string | null;
  requestedCamera?: CctvCameraRequest | null;
  onCurrent?: OnCurrent;
  /** 영상 위에 겹쳐 보일 안내(원격 시청 멈춤·다시 보기). */
  notice?: ReactNode;
}) {
  const [camera, setCamera] = useState(requestedCamera?.cameraId ?? '');
  const link = useCctvStore((s) => s.link);
  const relayIds = useCctvStore((s) => s.cameras);
  const { cameras, stateOf } = useCameraStates();
  const baseIds =
    attach && link === 'open' && relayIds.length ? relayIds : cameras.map((c) => c.id);
  const ids =
    requestedCamera?.cameraId && !baseIds.includes(requestedCamera.cameraId)
      ? [...baseIds, requestedCamera.cameraId]
      : baseIds;
  const selectedTarget = targetOfKey(selectedKey);
  const selectedPerson = selectedTarget?.type === 'person' ? selectedTarget.id : null;
  // 고른 사람의 지금 World ID. 병합으로 그 ID 가 다른 트랙의 별칭이 되면 바뀐다(-661).
  const personNow = useRealtimeStore((s) =>
    currentPersonId(s.world?.payload.tracks, selectedPerson),
  );
  useEffect(() => {
    const target = targetOfKey(selectedKey);
    const best = bestCamera(
      useRealtimeStore.getState().world?.payload.tracks,
      target?.type === 'person' ? { type: 'person', id: target.id } : null,
    );
    if (best) {
      // 외부 3D 선택이 바뀐 순간에만 카메라를 갱신한다.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setCamera(best);
    }
  }, [selectedKey]);
  // 고른 사람이 병합으로 다른 World ID 에 흡수되면 그 사람(생존 트랙) 기준으로 다시 고른다. 처음 나타나거나 사라질 때는
  // 그대로 둔다(카메라 마커로 고른 카메라를 덮지 않게).
  const lastPerson = useRef(personNow);
  useEffect(() => {
    const prev = lastPerson.current;
    lastPerson.current = personNow;
    if (!prev || !personNow || prev === personNow) return;
    const best = bestCamera(useRealtimeStore.getState().world?.payload.tracks, {
      type: 'person',
      id: personNow,
    });
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (best) setCamera(best);
  }, [personNow]);
  // 마운트 때 두 효과가 모두 돌므로 마커 요청이 뒤에 와야 사람 선택에 덮이지 않는다.
  useEffect(() => {
    if (!requestedCamera) return;
    // 3D 카메라 마커를 클릭하면 이미 열려 있는 창도 해당 카메라로 바꾼다.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCamera(requestedCamera.cameraId);
  }, [requestedCamera]);
  // 창은 늘 카메라 하나만 보인다. 고른 카메라가 없거나(처음 연 창) 중계 목록에 없으면 「카메라 1」(번호 순서의 첫 카메라)을
  // 보인다(S15P31A101-798). 목록을 아직 모르면 카메라를 지어내지 않고 안내를 보인다.
  const order = useCameraIds(ids);
  const current = ids.includes(camera) ? camera : (order.find((id) => ids.includes(id)) ?? ids[0]);
  useEffect(() => {
    onCurrent?.(current);
  }, [current, onCurrent]);
  // 창 높이는 영상 비율을 따른다(영상 밑에 빈 칸이 남지 않게). 해상도를 모르면 16:9 다.
  const aspect = useCctvStore((s) => {
    const view = current ? s.views[current] : undefined;
    return view?.width && view.height ? `${view.width} / ${view.height}` : DEFAULT_ASPECT;
  });
  const linkText = attach && link !== 'open' ? tileLabel(undefined, link) : null;
  return (
    <div className="relative w-full bg-surface-2" style={{ aspectRatio: aspect }}>
      {current && (
        <div key={current} className="size-full">
          {attach ? (
            <CameraTile
              cameraId={current}
              attach={attach}
              state={stateOf(current)}
              badgeCorner="bottom-left"
              className={FRAMELESS}
            />
          ) : (
            <StateTile
              cameraId={current}
              state={stateOf(current)}
              badgeCorner="bottom-left"
              className={FRAMELESS}
            />
          )}
        </div>
      )}
      {(linkText || !current || notice) && (
        // 상태 글은 영상 가운데에 겹쳐 둔다. 글 자리는 누르기를 통과시켜 영상을 끌어 창을 옮길 수 있다.
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-2 p-4">
          {linkText && (
            <p role="status" className={STATUS}>
              {linkText}
            </p>
          )}
          {!current && (
            <p role="status" className={STATUS}>
              카메라 연결 정보가 없습니다
            </p>
          )}
          {notice}
        </div>
      )}
    </div>
  );
}

/** 다시 보기 버튼 누르기가 창 끌기로 넘어가지 않게 한다. */
const keepPress = (e: ReactPointerEvent) => e.stopPropagation();

function AdminContent({
  selectedKey,
  requestedCamera,
  onCurrent,
}: {
  selectedKey: string | null;
  requestedCamera?: CctvCameraRequest | null;
  onCurrent?: OnCurrent;
}) {
  const { attach, resume } = useCctvPreview(env.VITE_CCTV_PREVIEW_URL, env.VITE_STOMP_URL);
  const link = useCctvStore((s) => s.link);
  const mode = useCctvStore((s) => s.mode);
  const pause = useCctvStore((s) => s.pause);
  const remote = useCctvStore((s) => s.remote);
  const notice = remoteNotice(mode, pause, remote);
  const resumable = Boolean(notice?.resumable) || link === 'stopped';
  return (
    <Content
      attach={attach}
      selectedKey={selectedKey}
      requestedCamera={requestedCamera}
      onCurrent={onCurrent}
      notice={
        notice || resumable ? (
          <>
            {notice && (
              <p role="status" className={STATUS}>
                {notice.text}
              </p>
            )}
            {resumable && (
              <Button
                size="sm"
                variant="secondary"
                className="pointer-events-auto"
                onPointerDown={keepPress}
                onClick={resume}
              >
                다시 보기
              </Button>
            )}
          </>
        ) : null
      }
    />
  );
}
const VIEW_FROM_CAMERA_LABEL = '3D 에서 이 방향으로 보기';
const NO_CAMERA_REASON = '보이는 카메라가 없습니다';

/** 영상 오른쪽 위의 작은 아이콘 버튼. 이름표(마우스 올림)와 화면 낭독기 이름을 단다. */
function OverlayIconButton({
  label,
  tip = label,
  onClick,
  children,
}: {
  label: string;
  tip?: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={label}
            className={OVERLAY_BUTTON}
            onClick={onClick}
          />
        }
      >
        {children}
      </TooltipTrigger>
      <TooltipContent>{tip}</TooltipContent>
    </Tooltip>
  );
}

/**
 * 오른쪽 위 「3D 에서 이 방향으로 보기」 아이콘 버튼(-626). 누르면 3D 시점을 창이 보이는 카메라 자리·방향으로 옮긴다.
 * 옮길 수 없으면 끄고 이유를 이름표로 보인다. 꺼진 버튼은 포인터를 받지 않으므로 감싼 자리에 이름표를 단다.
 */
function ViewFromCameraButton({
  cameraId,
  blockReason,
  onView,
}: {
  cameraId: string | undefined;
  blockReason: (cameraId: string) => string | null;
  onView: (cameraId: string) => void;
}) {
  const reasonId = useId();
  const reason = cameraId ? blockReason(cameraId) : NO_CAMERA_REASON;
  const icon = <View aria-hidden />;
  if (reason !== null || !cameraId) {
    return (
      <Tooltip>
        <TooltipTrigger render={<span tabIndex={0} className="inline-flex" />}>
          <Button
            variant="ghost"
            size="icon-sm"
            disabled
            aria-label={VIEW_FROM_CAMERA_LABEL}
            aria-describedby={reasonId}
            className={OVERLAY_BUTTON}
          >
            {icon}
          </Button>
          <span id={reasonId} className="sr-only">
            {reason}
          </span>
        </TooltipTrigger>
        <TooltipContent>{reason}</TooltipContent>
      </Tooltip>
    );
  }
  return (
    <OverlayIconButton label={VIEW_FROM_CAMERA_LABEL} onClick={() => onView(cameraId)}>
      {icon}
    </OverlayIconButton>
  );
}

/**
 * 떠 있는 CCTV 창(S15P31A101-798). 제목 줄·테두리·안쪽 여백 없이 카메라 영상 하나만 가장자리까지 채우고, 둥근 모서리와
 * 그림자만 둔다. 영상을 끌어 창을 옮기고(확대 중의 끌기는 영상 안 옮기기), 오른쪽 아래 모서리로 폭을 바꾸며 높이는 영상
 * 비율을 따른다. 닫기(X)·「3D 에서 이 방향으로 보기」·「3D 전체 보기」는 영상 오른쪽 위 아이콘이고, 지연 배지는 왼쪽 아래다.
 * 연결·원격 시청 안내는 영상 가운데에 겹친다. 접기는 없다(닫고 사람을 고르거나 3D 카메라 마커를 눌러 다시 연다).
 * 기본 자리는 오른쪽 아래이고 높이는 왼쪽 아래 평면 지도와 같다(-812). 3D 화면 가운데를 가리지 않게 한다. 끌어 옮긴 자리는
 * 아래쪽 기준이라 폭을 바꿔도 아래 가장자리가 그대로다.
 */
export function CctvFloat({
  onClose,
  onFullView,
  onViewFromCamera,
  viewFromCameraBlock = () => null,
  selectedKey,
  requestedCamera = null,
}: {
  onClose: () => void;
  /** 3D 가 카메라 시점 보기 중일 때만 넘긴다(-590). 누르면 3D 를 클릭 전 시점으로 돌린다. */
  onFullView?: () => void;
  /**
   * 「3D 에서 이 방향으로 보기」를 눌렀을 때 창이 보이는 카메라 ID 로 부른다(-626). 넘기지 않으면 버튼을 두지 않는다.
   * 카메라 마커를 눌러 창을 열 때는 시점을 옮기지 않고 이 버튼으로만 옮긴다.
   */
  onViewFromCamera?: (cameraId: string) => void;
  /** 그 카메라로 시점을 옮길 수 없으면 이유(이름표 문구), 옮길 수 있으면 null(-626). */
  viewFromCameraBlock?: (cameraId: string) => string | null;
  selectedKey: string | null;
  requestedCamera?: CctvCameraRequest | null;
}) {
  const [initial] = useState(readSettings);
  // width 가 null 이면 기본 폭(평면 지도 높이 기준 CSS 값)이다. 처음 배치 뒤에는 실제 px 로 바뀐다.
  const [size, setSize] = useState<{ width: number | null; height: number }>(() => {
    const width = initial.videoWidth ?? null;
    return { width, height: width === null ? 0 : Math.round((width * 9) / 16) };
  });
  const box = useRef<HTMLElement>(null);
  const saveSpot = useCallback((spot: FloatSpot) => saveSettings({ spot }), []);
  const spot = useFloatSpot(box, 'bottom', FLOAT_MARGIN, initial.spot ?? null, saveSpot);
  // 이벤트 패널이 이 창을 비켜 서도록 자리를 알린다(끄는 동안은 알리지 않고 놓은 뒤에).
  const layoutKey = `${spot.style?.left},${spot.style?.bottom},${size.width}x${size.height}`;
  useReportCctvFloatBox(box, { dragging: spot.dragging, layoutKey });
  const isAdmin = useCurrentRole() === 'ADMIN';
  // 창이 보이는 카메라(「3D 에서 이 방향으로 보기」가 쓴다).
  const [currentCamera, setCurrentCamera] = useState<string | undefined>(requestedCamera?.cameraId);
  // 보이는 카메라의 영상 비율(창 안 영상 칸과 같은 값). 기본 폭 계산에 쓴다.
  const aspect = useCctvStore((s) => {
    const view = currentCamera ? s.views[currentCamera] : undefined;
    return view?.width && view.height ? `${view.width} / ${view.height}` : DEFAULT_ASPECT;
  });
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      const { offsetWidth: width, offsetHeight: height } = el;
      if (!width || !height) return;
      // 사용자가 모서리로 폭을 바꾸면 브라우저가 인라인 폭을 px 로 쓴다. 그때만 px 폭을 쓰고 저장한다. 기본 폭(CSS 값)이면
      // 높이만 적어 두고 저장하지 않는다(화면 높이가 바뀌면 평면 지도와 함께 다시 맞춰진다, -812).
      if (!el.style.width.endsWith('px')) {
        setSize((prev) => (prev.height === height ? prev : { ...prev, height }));
        return;
      }
      setSize((prev) =>
        prev.width === width && prev.height === height ? prev : { width, height },
      );
      saveSettings({ videoWidth: width });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return (
    <section
      ref={box}
      aria-label="떠 있는 CCTV"
      style={{ ...spot.style, width: size.width ?? defaultWidthCss(aspect), resize: 'horizontal' }}
      className="absolute right-3.5 bottom-3.5 z-[7] max-w-[calc(100%-2rem)] min-w-[280px] overflow-hidden rounded-xl bg-card shadow-float"
    >
      <div
        {...spot.handlers}
        role="group"
        aria-label="CCTV 영상, 끌어서 창 옮기기"
        className={cn('touch-none select-none', spot.dragging ? 'cursor-grabbing' : 'cursor-grab')}
      >
        {isAdmin ? (
          <AdminContent
            selectedKey={selectedKey}
            requestedCamera={requestedCamera}
            onCurrent={setCurrentCamera}
          />
        ) : (
          <Content
            selectedKey={selectedKey}
            requestedCamera={requestedCamera}
            onCurrent={setCurrentCamera}
          />
        )}
      </div>
      {/* 끌기 자리(영상) 밖의 형제라 버튼을 눌러도 창 끌기가 시작되지 않는다. */}
      <TooltipProvider>
        <div className="absolute top-2 right-2 flex items-center gap-1">
          {onViewFromCamera && (
            <ViewFromCameraButton
              cameraId={currentCamera}
              blockReason={viewFromCameraBlock}
              onView={onViewFromCamera}
            />
          )}
          {onFullView && (
            <OverlayIconButton label="3D 전체 보기" onClick={onFullView}>
              <Maximize2 aria-hidden />
            </OverlayIconButton>
          )}
          <OverlayIconButton label="떠 있는 CCTV 닫기" tip="닫기" onClick={onClose}>
            <X aria-hidden />
          </OverlayIconButton>
        </div>
      </TooltipProvider>
    </section>
  );
}
