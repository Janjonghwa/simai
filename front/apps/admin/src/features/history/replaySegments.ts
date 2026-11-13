import type { VideoSegment } from '@/features/history/types';
import type { ReplayStatus } from '@/features/viewer3d';

// 다시 보기에서 CCTV 칸에 상시 영상 조각을 틀기 위한 계산. 화면 없이 시험한다.

/** 재생 중 영상이 재생 시각과 이만큼(초) 넘게 어긋나면 위치를 다시 맞춘다. 매 tick 마다 맞추면 영상이 끊긴다. */
export const DRIFT_S = 0.5;

/**
 * 멈춤 중에는 한 프레임(25 fps, 40 ms)보다 어긋나면 맞춘다. 멈춘 화면을 3D 다시 보기와 견줘 볼 때 같은 프레임이어야 한다
 * (게이트웨이 녹화 mp4 는 PTS 가 촬영 시각이라 currentTime 이 그대로 그 시각의 프레임이다, S15P31A101-668).
 */
export const PAUSED_DRIFT_S = 0.04;

/** <video> 가 받는 재생 속도 범위. 넘는 배속은 가까운 값으로 둔다. */
const MIN_RATE = 0.0625;
const MAX_RATE = 16;

/** 재생 시각을 덮는 조각. 카메라마다 가장 늦게 시작한 것 하나이고, 지운 조각은 뺀다. 카메라 이름순이다. */
export function activeSegments(segments: readonly VideoSegment[], playhead: number | null) {
  if (playhead === null) return [];
  const latest = new Map<string, VideoSegment>();
  for (const segment of segments) {
    if (segment.deleted_at_ms != null) continue;
    if (playhead < segment.segment_start_capture_ms || playhead >= segment.segment_end_capture_ms) {
      continue;
    }
    const held = latest.get(segment.camera_id);
    if (!held || segment.segment_start_capture_ms > held.segment_start_capture_ms) {
      latest.set(segment.camera_id, segment);
    }
  }
  return [...latest.values()].sort((a, b) => a.camera_id.localeCompare(b.camera_id));
}

/** 조각 안의 재생 위치(초). */
export const segmentOffsetS = (segment: VideoSegment, playhead: number) =>
  (playhead - segment.segment_start_capture_ms) / 1000;

/** <video> 중 이 계산이 읽고 쓰는 칸. */
export interface VideoLike {
  currentTime: number;
  playbackRate: number;
  paused: boolean;
  play: () => Promise<void>;
  pause: () => void;
}

/** 영상을 재생 시각·재생 중·배속에 맞춘다. 기록이 비어 시각이 없으면 두고 본다. 오차는 재생 중 DRIFT_S, 멈춤 중 PAUSED_DRIFT_S 이내다. */
export function syncVideo(
  video: VideoLike,
  segment: VideoSegment,
  status: Pick<ReplayStatus, 'playhead' | 'playing' | 'speed'>,
): void {
  if (status.playhead === null) return;
  const target = segmentOffsetS(segment, status.playhead);
  const drift = status.playing ? DRIFT_S : PAUSED_DRIFT_S;
  if (Math.abs(video.currentTime - target) > drift) video.currentTime = target;
  const rate = Math.min(MAX_RATE, Math.max(MIN_RATE, status.speed));
  if (video.playbackRate !== rate) video.playbackRate = rate;
  if (status.playing && video.paused) video.play().catch(() => {});
  else if (!status.playing && !video.paused) video.pause();
}
