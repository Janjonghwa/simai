import {
  activeSegments,
  DRIFT_S,
  segmentOffsetS,
  syncVideo,
  type VideoLike,
} from '@/features/history/replaySegments';
import { videoSegmentsSchema, type VideoSegment } from '@/features/history/types';
import { describe, expect, it, vi } from 'vitest';

const T = 1_760_342_400_000;

function segment(change: Partial<VideoSegment> = {}): VideoSegment {
  return {
    segment_id: 's-1',
    camera_id: 'cam_A',
    segment_start_capture_ms: T,
    segment_end_capture_ms: T + 60_000,
    ...change,
  };
}

describe('videoSegmentsSchema', () => {
  it('형식이 틀린 조각 하나는 빼고 나머지를 보인다', () => {
    const parsed = videoSegmentsSchema.parse({
      segments: [
        { ...segment(), container: 'mp4', content_type: 'video/mp4', size_bytes: 1 },
        { segment_id: 'broken' },
      ],
    });
    expect(parsed.segments.map((s) => s.segment_id)).toEqual(['s-1']);
  });
});

describe('activeSegments', () => {
  it('재생 시각을 덮는 조각만 카메라 이름순으로 준다(끝 시각은 덮지 않는다)', () => {
    const segments = [
      segment({ segment_id: 'b', camera_id: 'cam_B' }),
      segment({ segment_id: 'a', camera_id: 'cam_A' }),
      segment({
        segment_id: 'later',
        camera_id: 'cam_C',
        segment_start_capture_ms: T + 60_000,
        segment_end_capture_ms: T + 120_000,
      }),
    ];
    expect(activeSegments(segments, T + 10_000).map((s) => s.segment_id)).toEqual(['a', 'b']);
    expect(activeSegments(segments, T).map((s) => s.segment_id)).toEqual(['a', 'b']);
    expect(activeSegments(segments, T + 60_000).map((s) => s.segment_id)).toEqual(['later']);
    expect(activeSegments(segments, T - 1)).toEqual([]);
  });

  it('1분 조각이 이어지면 경계에서 다음 조각으로 넘어간다', () => {
    const segments = [
      segment({ segment_id: 'm1' }),
      segment({
        segment_id: 'm2',
        segment_start_capture_ms: T + 60_000,
        segment_end_capture_ms: T + 120_000,
      }),
    ];
    expect(activeSegments(segments, T + 59_999).map((s) => s.segment_id)).toEqual(['m1']);
    expect(activeSegments(segments, T + 60_000).map((s) => s.segment_id)).toEqual(['m2']);
  });

  it('같은 카메라에 겹치면 더 늦게 시작한 조각을, 지운 조각은 뺀다', () => {
    const segments = [
      segment({ segment_id: 'old' }),
      segment({
        segment_id: 'new',
        segment_start_capture_ms: T + 20_000,
        segment_end_capture_ms: T + 80_000,
      }),
      segment({ segment_id: 'gone', camera_id: 'cam_B', deleted_at_ms: T }),
    ];
    expect(activeSegments(segments, T + 30_000).map((s) => s.segment_id)).toEqual(['new']);
  });

  it('재생 시각이 없으면(기록이 비었음) 없다', () => {
    expect(activeSegments([segment()], null)).toEqual([]);
  });
});

describe('syncVideo', () => {
  const video = (change: Partial<VideoLike> = {}): VideoLike => ({
    currentTime: 0,
    playbackRate: 1,
    paused: true,
    play: vi.fn(() => Promise.resolve()),
    pause: vi.fn(),
    ...change,
  });

  it('조각 안의 위치는 재생 시각에서 조각 시작을 뺀 값이다', () => {
    expect(segmentOffsetS(segment(), T + 12_500)).toBe(12.5);
  });

  it('어긋남이 허용 안이면 위치를 두고, 넘으면 다시 맞춘다', () => {
    const near = video({ currentTime: 10 });
    syncVideo(near, segment(), {
      playhead: T + 10_000 + DRIFT_S * 1000 - 1,
      playing: false,
      speed: 1,
    });
    expect(near.currentTime).toBe(10);

    const far = video({ currentTime: 10 });
    syncVideo(far, segment(), { playhead: T + 20_000, playing: false, speed: 1 });
    expect(far.currentTime).toBe(20);
  });

  it('재생 중이면 틀고 멈추면 세우고, 배속을 맞춘다', () => {
    const v = video();
    syncVideo(v, segment(), { playhead: T, playing: true, speed: 4 });
    expect(v.play).toHaveBeenCalledTimes(1);
    expect(v.playbackRate).toBe(4);

    const playing = video({ paused: false });
    syncVideo(playing, segment(), { playhead: T, playing: false, speed: 1 });
    expect(playing.pause).toHaveBeenCalledTimes(1);
  });

  it('영상이 받는 배속 범위로 자른다', () => {
    const fast = video();
    syncVideo(fast, segment(), { playhead: T, playing: false, speed: 100 });
    expect(fast.playbackRate).toBe(16);
    const slow = video();
    syncVideo(slow, segment(), { playhead: T, playing: false, speed: 0.001 });
    expect(slow.playbackRate).toBe(0.0625);
  });

  it('재생이 막혀도(자동 재생 정책) 오류를 던지지 않는다', async () => {
    const v = video({ play: vi.fn(() => Promise.reject(new Error('NotAllowedError'))) });
    expect(() => syncVideo(v, segment(), { playhead: T, playing: true, speed: 1 })).not.toThrow();
    await Promise.resolve();
  });

  it('재생 시각이 없으면 아무것도 건드리지 않는다', () => {
    const v = video({ currentTime: 3 });
    syncVideo(v, segment(), { playhead: null, playing: true, speed: 2 });
    expect(v.currentTime).toBe(3);
    expect(v.play).not.toHaveBeenCalled();
  });
});
