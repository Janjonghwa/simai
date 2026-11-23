import { LoadingDots } from '@/components/LoadingDots';
import { CctvWall, type WallRest } from '@/features/cctv/CctvWall';
import { TILE_FRAME, tileZoom } from '@/features/cctv/tileStyle';
import { loadPlayableClip } from '@/features/clips/clipMedia';
import { CONTAINER_MP4 } from '@/features/clips/constants';
import { formatTime } from '@/features/events/events';
import {
  syncTileVideo,
  tileVideos,
  uploadLine,
  type TileVideoRole,
  type UploadProgress,
} from '@/features/history/replaySegments';
import {
  VIDEO_URL_CACHE_MS,
  videoUrlQuery,
  type SegmentBlobCache,
} from '@/features/history/segmentPrefetch';
import type { VideoSegment } from '@/features/history/types';
import { useReplayStatus } from '@/features/history/useWorkReplay';
import type { ReplaySpatialSource, ReplayStatus } from '@/features/viewer3d';
import { useCameraNames } from '@/hooks/useCameraNames';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { cn } from '@simai/ui/lib/utils';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

const PILL_LOOK =
  'absolute rounded-md bg-black/55 px-2 py-0.5 text-xs text-slate-100 backdrop-blur-[2px]';
const PILL = cn(PILL_LOOK, 'top-2');
/** 다른 시각으로 넘어가는 동안 앞 화면 위에 띄우는 작은 「불러오는 중」(S15P31A101-859). 칸 이름(왼쪽 위)·영상 시각(오른쪽 위)과 겹치지 않게 왼쪽 아래. */
const LOADING_PILL = (
  <span role="status" className={cn(PILL_LOOK, 'bottom-2 left-2')}>
    <LoadingDots label="불러오는 중" />
  </span>
);
const CENTER_LINE = 'absolute inset-0 flex items-center justify-center text-sm text-slate-400';

type OnFailed = (segmentId: string, message?: string) => void;

/**
 * 조각 재생 주소. blobUrl(위험 이벤트 조각을 미리 받아 둔 것, S15P31A101-859)이 있으면 그것을 그대로 튼다.
 * 없으면 서명 주소(서버 5분)를 받는다. 서명 주소는 4분(VIDEO_URL_CACHE_MS) 보관해 이미 본 시각으로 돌아가도 다시 받지 않고,
 * 칸이 붙을 때 그보다 오래된 주소는 쓰지 않고 새로 받는다(만료 직전 주소로 시작하지 않게). 영상 형식은 클립과 같은 규칙으로 읽는다
 * (게이트웨이 녹화는 mp4 라 서명 주소를 그대로 튼다, S15P31A101-668). h264-annexb 를 감싼 blob 은 보관하지 않고 칸마다 만들어
 * 칸이 사라지면 해제한다(서명 주소 보관과 blob 해제 시점이 엇갈리지 않게 쿼리를 나눴다).
 */
function useSegmentSource(segment: VideoSegment, blobUrl: string | undefined) {
  const segmentId = segment.segment_id;
  const [mountedAt] = useState(Date.now);
  const urlQuery = useQuery({ ...videoUrlQuery(api, segmentId), enabled: !blobUrl });
  const info =
    urlQuery.data && urlQuery.dataUpdatedAt + VIDEO_URL_CACHE_MS > mountedAt
      ? urlQuery.data
      : undefined;
  const wrapQuery = useQuery({
    queryKey: ['history', 'video-wrap', segmentId, info?.url],
    queryFn: ({ signal }) =>
      loadPlayableClip(
        { ...info!, clip_id: info!.segment_id },
        {
          clip_start_capture_ms: segment.segment_start_capture_ms,
          clip_end_capture_ms: segment.segment_end_capture_ms,
        },
        signal,
      ),
    enabled: !blobUrl && info !== undefined && info.container !== CONTAINER_MP4,
    gcTime: 0,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });
  const wrapped = wrapQuery.data;
  // 감싼 영상의 blob 주소는 새 영상으로 바뀌거나 칸이 사라질 때 해제한다.
  useEffect(() => () => wrapped?.release(), [wrapped]);
  const url =
    blobUrl ?? (info?.container === CONTAINER_MP4 ? info.url : info ? wrapped?.url : undefined);
  // 보관해 둔 실패는 칸이 붙을 때 다시 받는다(받는 중에는 실패로 보지 않는다)
  const failed = !blobUrl && ((urlQuery.isError && !urlQuery.isFetching) || wrapQuery.isError);
  const error = urlQuery.error ?? wrapQuery.error;
  const errorLine =
    error instanceof ApiError && error.status === 403
      ? '영상 조회 권한이 없습니다.'
      : error instanceof ApiError && error.status === 401
        ? '로그인이 만료되었습니다. 다시 로그인해 주세요.'
        : error instanceof ApiError && error.status === 410
          ? '영상 보관 기간이 만료되었습니다.'
          : '영상을 불러오지 못했습니다. 네트워크 연결을 확인해 주세요.';
  return { url, failed, refetch: urlQuery.refetch, errorLine };
}

/**
 * 칸 하나의 영상 바꾸기. 첫 화면이 나온 조각 id 와 마지막으로 보인 조각을 들고, 그릴 영상들과 보일 조각을 고른다(tileVideos).
 * onReady 는 영상이 첫 화면을 냈을 때(true)와 비거나 사라졌을 때(false) 부른다.
 */
function useSegmentSwap(live: VideoSegment, upcoming: VideoSegment | undefined) {
  const [ready, setReady] = useState<ReadonlySet<string>>(() => new Set());
  const [held, setHeld] = useState<VideoSegment | null>(null);
  // 지금 조각이 첫 화면을 내면 그 조각이 마지막으로 보인 조각이 된다(그리는 중에 맞춰 이전 조각을 한 번 더 그리지 않는다)
  if (ready.has(live.segment_id) && held?.segment_id !== live.segment_id) setHeld(live);
  const onReady = useCallback((segmentId: string, isReady: boolean) => {
    setReady((current) => {
      if (current.has(segmentId) === isReady) return current;
      const next = new Set(current);
      if (isReady) next.add(segmentId);
      else next.delete(segmentId);
      return next;
    });
  }, []);
  return { ...tileVideos(live, held, upcoming, ready), onReady };
}

/** 칸 안 영상 하나. 맡은 일(role)대로 재생 시각을 따르거나 멈춰 두고, 보일 때만 불투명하다. */
function SegmentVideo({
  segment,
  role,
  status,
  shown,
  onReady,
  onFailed,
  blobs,
}: {
  segment: VideoSegment;
  role: TileVideoRole;
  status: ReplayStatus;
  shown: boolean;
  onReady: (segmentId: string, isReady: boolean) => void;
  onFailed?: OnFailed;
  blobs?: SegmentBlobCache | null;
}) {
  const segmentId = segment.segment_id;
  // 미리 받아 둔 조각이면 blob 주소로 튼다. 붙을 때 한 번만 고른다(재생 중에 주소를 바꾸면 영상을 처음부터 다시 읽는다).
  const [blobUrl, setBlobUrl] = useState(() => blobs?.get(segmentId));
  const { url, failed, refetch, errorLine } = useSegmentSource(segment, blobUrl);
  const video = useRef<HTMLVideoElement | null>(null);
  const lastVideo = useRef<HTMLVideoElement | null>(null);
  const [seeking, setSeeking] = useState(false);
  // 서명 주소가 만료돼(5분) 이어 받기가 막히면 <video> 가 오류를 낸다. 주소를 한 번 다시 받아 같은 재생 시각으로 잇고, 그래도 안 되면 뺀다.
  const [renewed, setRenewed] = useState(false);
  const [videoTime, setVideoTime] = useState<number | null>(null);

  useEffect(() => {
    if (video.current && url) syncTileVideo(video.current, segment, role, status);
  }, [segment, role, status, url]);

  useEffect(() => () => onReady(segmentId, false), [segmentId, onReady]);

  // 칸에서 빠지면 영상을 비워 받기·디코더를 바로 놓는다. 떼어 낸 <video> 는 GC 까지 디코더를 쥐고 있어, 뛰어넘을 때마다 쌓이면
  // 새 칸의 첫 화면이 늦었다(S15P31A101-859 측정).
  useEffect(
    () => () => {
      const element = lastVideo.current;
      // 아직 문서에 붙어 있으면 실제로 빠진 것이 아니다(개발 모드 StrictMode 의 effect 두 번 실행)
      if (!element || element.isConnected) return;
      element.removeAttribute('src');
      element.load();
    },
    [],
  );

  // 주소를 받지 못했으면 이 조각은 뺀다(남은 조각이 없으면 3D 다시 보기로 돌아간다).
  useEffect(() => {
    if (failed) onFailed?.(segmentId, errorLine);
  }, [failed, onFailed, segmentId, errorLine]);

  const onVideoError = () => {
    // blob 이 해제됐거나 읽지 못하면 서명 주소로 다시 튼다
    if (blobUrl) {
      setBlobUrl(undefined);
      return;
    }
    if (renewed) {
      onFailed?.(
        segmentId,
        '영상을 재생하지 못했습니다. 네트워크 연결 또는 영상 형식을 확인해 주세요.',
      );
      return;
    }
    setRenewed(true);
    void refetch();
  };

  if (!url || failed) return null;
  return (
    <>
      <video
        ref={(element) => {
          video.current = element;
          if (element) lastVideo.current = element;
        }}
        src={url}
        muted
        playsInline
        preload="auto"
        // 영상은 재생 시각이 끌고 간다. 영상 안의 위치가 아니라 기록의 시각이 기준이다.
        onLoadedMetadata={() => {
          // 새 주소로 다시 읽혔으면 다음 만료 때 또 한 번 다시 받을 수 있다
          setRenewed(false);
          if (video.current) syncTileVideo(video.current, segment, role, status);
        }}
        onLoadedData={() => {
          onReady(segmentId, true);
          if (video.current)
            setVideoTime(segment.segment_start_capture_ms + video.current.currentTime * 1000);
        }}
        onTimeUpdate={(event) =>
          setVideoTime(segment.segment_start_capture_ms + event.currentTarget.currentTime * 1000)
        }
        onEmptied={() => onReady(segmentId, false)}
        onSeeking={() => setSeeking(true)}
        onSeeked={() => setSeeking(false)}
        onError={onVideoError}
        className={cn('absolute inset-0 size-full object-cover', !shown && 'opacity-0')}
      />
      {/* 같은 조각 안에서 옮길 때: 앞 화면이 그대로 보이는 동안 */}
      {shown && role === 'live' && seeking && LOADING_PILL}
      {shown &&
        videoTime !== null &&
        status.playhead !== null &&
        Math.abs(videoTime - status.playhead) > 1000 && (
          <span className={cn(PILL, 'right-2 font-mono')}>{formatTime(videoTime)}</span>
        )}
    </>
  );
}

/** 카메라 칸 틀: 누르면 크게 보고, 왼쪽 위에 카메라 이름, 오른쪽 위에 재생 시각을 단다. */
function TileFrame({
  label,
  cameraId,
  focused,
  onToggle,
  children,
}: {
  label: string;
  cameraId: string;
  focused: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={`${label} ${focused ? '작게 보기' : '크게 보기'}`}
      className={cn(TILE_FRAME, 'bg-slate-900', tileZoom(focused))}
    >
      {children}
      <span className={cn(PILL, 'left-2 font-mono font-bold')} title={cameraId}>
        {label}
      </span>
    </button>
  );
}

function SegmentTile({
  segment,
  upcoming,
  replay,
  focused,
  onToggle,
  onFailed,
  label,
  blobs,
  onShown,
}: {
  segment: VideoSegment;
  upcoming: VideoSegment | undefined;
  replay: ReplaySpatialSource | null;
  focused: boolean;
  onToggle: () => void;
  onFailed?: OnFailed;
  label: string;
  blobs?: SegmentBlobCache | null;
  onShown: (cameraId: string, shown: boolean) => void;
}) {
  const status = useReplayStatus(replay);
  const { videos, shownId, onReady } = useSegmentSwap(segment, upcoming);
  const cameraId = segment.camera_id;
  const anyShown = shownId !== null;
  useEffect(() => {
    onShown(cameraId, anyShown);
    return () => onShown(cameraId, false);
  }, [cameraId, anyShown, onShown]);

  return (
    <TileFrame label={label} cameraId={segment.camera_id} focused={focused} onToggle={onToggle}>
      {videos.map((video) => (
        <SegmentVideo
          key={video.segment.segment_id}
          segment={video.segment}
          role={video.role}
          status={status}
          shown={video.segment.segment_id === shownId}
          onReady={onReady}
          onFailed={onFailed}
          blobs={blobs}
        />
      ))}
      {/* 다른 조각으로 넘어가는 동안: 마지막으로 보이던 화면을 멈춘 채 두고 그 위에 작게 알린다 */}
      {shownId !== null && shownId !== segment.segment_id && LOADING_PILL}
      {/* 목록에 있는(이미 올라온) 조각의 영상을 받는 동안 */}
      {shownId === null && (
        <span className={CENTER_LINE}>
          <LoadingDots label="영상을 불러오는 중입니다" />
        </span>
      )}
    </TileFrame>
  );
}

/** 녹화는 있지만 재생 시각의 조각이 아직 올라오지 않은 카메라 칸. 올라오면 SegmentTile 로 바뀐다. */
function UploadWaitingTile({
  cameraId,
  upload,
  focused,
  onToggle,
  label,
}: {
  cameraId: string;
  upload: UploadProgress | null;
  focused: boolean;
  onToggle: () => void;
  label: string;
}) {
  return (
    <TileFrame label={label} cameraId={cameraId} focused={focused} onToggle={onToggle}>
      <span className={CENTER_LINE}>
        <LoadingDots label={upload ? uploadLine(upload) : '업로드 중'} />
      </span>
    </TileFrame>
  );
}

/**
 * 재생 시각을 덮는 영상 조각을 카메라 칸마다 튼다(CCTV 영상). 칸을 누르면 크게 본다.
 * upcoming(곧 덮을 조각)은 칸마다 미리 올려 두었다가 조각 경계에서 검은 화면 없이 바꾼다.
 * 조각 영상을 끝내 받지 못하면 onFailed 로 알린다(부르는 쪽이 그 조각을 빼고, 남은 게 없으면 3D 다시 보기로 돌아간다).
 * waiting 카메라(녹화 업로드 중이라 재생 시각의 조각이 아직 없음)는 「업로드 중」 칸으로 함께 둔다(S15P31A101-801).
 */
export function ReplayVideoWall({
  segments,
  upcoming,
  waiting = [],
  upload = null,
  replay,
  onFailed,
  rest,
  blobs = null,
  onFirstFrames,
}: {
  segments: readonly VideoSegment[];
  upcoming: readonly VideoSegment[];
  /** 재생 시각의 조각이 아직 올라오지 않은 카메라. */
  waiting?: readonly string[];
  /** 녹화 업로드 진행(「업로드 중 12/28」). 모르면 null. */
  upload?: UploadProgress | null;
  replay: ReplaySpatialSource | null;
  onFailed?: OnFailed;
  /** 크게 볼 때 나머지 칸을 놓는 곳(CctvWall). */
  rest?: WallRest;
  /** 미리 받아 둔 조각(위험 이벤트, S15P31A101-859). 있으면 서명 주소 대신 blob 주소로 튼다. */
  blobs?: SegmentBlobCache | null;
  /** 영상 칸이 모두 첫 화면을 냈다(미리 받기를 이 뒤에 시작한다). */
  onFirstFrames?: () => void;
}) {
  const byCamera = new Map(segments.map((segment) => [segment.camera_id, segment]));
  const [shownCameras, setShownCameras] = useState<ReadonlySet<string>>(() => new Set());
  const onShown = useCallback((cameraId: string, shown: boolean) => {
    setShownCameras((current) => {
      if (current.has(cameraId) === shown) return current;
      const next = new Set(current);
      if (shown) next.add(cameraId);
      else next.delete(cameraId);
      return next;
    });
  }, []);
  const allShown = byCamera.size > 0 && [...byCamera.keys()].every((id) => shownCameras.has(id));
  useEffect(() => {
    if (allShown) onFirstFrames?.();
  }, [allShown, onFirstFrames]);
  const nextByCamera = new Map(upcoming.map((segment) => [segment.camera_id, segment]));
  // 영상 칸과 업로드 중 칸을 카메라 이름순으로 섞는다(조각이 올라와도 칸 자리가 그대로다)
  const ids = [...new Set([...byCamera.keys(), ...waiting])].sort((a, b) => a.localeCompare(b));
  const label = useCameraNames(ids);
  return (
    <CctvWall
      ids={ids}
      rest={rest}
      tile={(id, wall) => {
        const segment = byCamera.get(id);
        if (segment) {
          return (
            <SegmentTile
              segment={segment}
              upcoming={nextByCamera.get(id)}
              label={label(segment.camera_id)}
              replay={replay}
              focused={wall.focused}
              onToggle={wall.toggle}
              onFailed={onFailed}
              blobs={blobs}
              onShown={onShown}
            />
          );
        }
        return (
          waiting.includes(id) && (
            <UploadWaitingTile
              cameraId={id}
              upload={upload}
              label={label(id)}
              focused={wall.focused}
              onToggle={wall.toggle}
            />
          )
        );
      }}
    />
  );
}
