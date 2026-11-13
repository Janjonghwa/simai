import { CctvWall } from '@/features/cctv/CctvWall';
import { TILE_FRAME, tileZoom } from '@/features/cctv/tileStyle';
import { loadPlayableClip } from '@/features/clips/clipMedia';
import { formatTime } from '@/features/events/events';
import { requestVideoUrl } from '@/features/history/api';
import { syncVideo } from '@/features/history/replaySegments';
import type { VideoSegment } from '@/features/history/types';
import { useReplayStatus } from '@/features/history/useWorkReplay';
import type { ReplaySpatialSource } from '@/features/viewer3d';
import { useCameraNames } from '@/hooks/useCameraNames';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { cn } from '@simai/ui/lib/utils';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

const PILL =
  'absolute top-2 rounded-md bg-black/55 px-2 py-0.5 text-xs text-slate-100 backdrop-blur-[2px]';

/**
 * 조각 재생 주소(서명 주소는 5분). 이 칸이 떠 있는 동안만 받고, 칸이 사라지면 버린다. 영상 형식은 클립과 같은 규칙으로 읽는다
 * (게이트웨이 녹화는 mp4 라 서명 주소를 그대로 튼다, S15P31A101-668).
 */
function useSegmentSource(segment: VideoSegment) {
  const query = useQuery({
    queryKey: ['history', 'video-source', segment.segment_id],
    queryFn: async ({ signal }) => {
      const info = await requestVideoUrl(api, segment.segment_id, signal);
      return loadPlayableClip(
        { ...info, clip_id: info.segment_id },
        {
          clip_start_capture_ms: segment.segment_start_capture_ms,
          clip_end_capture_ms: segment.segment_end_capture_ms,
        },
        signal,
      );
    },
    gcTime: 0,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });
  const source = query.data;
  // 감싼 영상의 blob 주소는 새 영상으로 바뀌거나 칸이 사라질 때 해제한다.
  useEffect(() => () => source?.release(), [source]);
  const error = query.error;
  const errorLine =
    error instanceof ApiError && error.status === 403
      ? '영상 조회 권한이 없습니다.'
      : error instanceof ApiError && error.status === 401
        ? '로그인이 만료되었습니다. 다시 로그인해 주세요.'
        : error instanceof ApiError && error.status === 410
          ? '영상 보관 기간이 만료되었습니다.'
          : '영상을 불러오지 못했습니다. 네트워크 연결을 확인해 주세요.';
  return { url: source?.url, failed: query.isError, refetch: query.refetch, errorLine };
}

function SegmentTile({
  segment,
  replay,
  focused,
  onToggle,
  onFailed,
  label,
}: {
  segment: VideoSegment;
  replay: ReplaySpatialSource | null;
  focused: boolean;
  onToggle: () => void;
  onFailed?: (segmentId: string, message?: string) => void;
  label: string;
}) {
  const { url, failed, refetch, errorLine } = useSegmentSource(segment);
  const status = useReplayStatus(replay);
  const video = useRef<HTMLVideoElement>(null);
  // 서명 주소가 만료돼(5분) 이어 받기가 막히면 <video> 가 오류를 낸다. 주소를 한 번 다시 받아 같은 재생 시각으로 잇고, 그래도 안 되면 뺀다.
  const [renewed, setRenewed] = useState(false);

  useEffect(() => {
    if (video.current && url) syncVideo(video.current, segment, status);
  }, [segment, status, url]);

  // 주소를 받지 못했으면 이 조각은 뺀다(남은 조각이 없으면 3D 다시 보기로 돌아간다).
  useEffect(() => {
    if (failed) onFailed?.(segment.segment_id, errorLine);
  }, [failed, onFailed, segment.segment_id, errorLine]);

  const onVideoError = () => {
    if (renewed) {
      onFailed?.(
        segment.segment_id,
        '영상을 재생하지 못했습니다. 네트워크 연결 또는 영상 형식을 확인해 주세요.',
      );
      return;
    }
    setRenewed(true);
    void refetch();
  };

  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={`${label} ${focused ? '작게 보기' : '크게 보기'}`}
      className={cn(TILE_FRAME, 'bg-slate-900', tileZoom(focused))}
    >
      {url && !failed ? (
        <video
          ref={video}
          src={url}
          muted
          playsInline
          // 영상은 재생 시각이 끌고 간다. 영상 안의 위치가 아니라 기록의 시각이 기준이다.
          onLoadedMetadata={() => {
            // 새 주소로 다시 읽혔으면 다음 만료 때 또 한 번 다시 받을 수 있다
            setRenewed(false);
            if (video.current) syncVideo(video.current, segment, status);
          }}
          onError={onVideoError}
          className="size-full object-cover"
        />
      ) : (
        <span className="flex size-full items-center justify-center text-sm text-slate-400">
          {failed ? '영상을 받지 못했습니다' : '영상을 불러오는 중입니다…'}
        </span>
      )}
      <span className={cn(PILL, 'left-2 font-mono font-bold')} title={segment.camera_id}>
        {label}
      </span>
      <span className={cn(PILL, 'right-2 font-mono')}>
        {status.playhead !== null ? formatTime(status.playhead) : ''}
      </span>
    </button>
  );
}

/**
 * 재생 시각을 덮는 영상 조각을 카메라 칸마다 튼다(CCTV 영상). 칸을 누르면 크게 본다.
 * 조각 영상을 끝내 받지 못하면 onFailed 로 알린다(부르는 쪽이 그 조각을 빼고, 남은 게 없으면 3D 다시 보기로 돌아간다).
 */
export function ReplayVideoWall({
  segments,
  replay,
  onFailed,
}: {
  segments: readonly VideoSegment[];
  replay: ReplaySpatialSource | null;
  onFailed?: (segmentId: string, message?: string) => void;
}) {
  const label = useCameraNames(segments.map((segment) => segment.camera_id));
  const byCamera = new Map(segments.map((segment) => [segment.camera_id, segment]));
  return (
    <CctvWall
      ids={[...byCamera.keys()]}
      tile={(id, wall) => {
        const segment = byCamera.get(id);
        return (
          segment && (
            <SegmentTile
              segment={segment}
              label={label(segment.camera_id)}
              replay={replay}
              focused={wall.focused}
              onToggle={wall.toggle}
              onFailed={onFailed}
            />
          )
        );
      }}
    />
  );
}
