import { create } from 'zustand';

// CCTV 로컬 미리보기 화면 상태. 프레임마다 바꾸지 않는다: 그리기는 캔버스가 직접 하고, 여기에는 연결·카메라 단계와
// 마지막으로 그린 프레임의 촬영 시각(카메라마다 0.25초에 한 번까지)만 둔다.

/**
 * connecting: 여는 중(처음·다시), open: 중계와 이어짐, unavailable: 붙지 못함(현장 노트북이 아니거나 중계 미리보기가 꺼짐),
 * unsupported: 이 브라우저에 WebCodecs VideoDecoder 가 없다.
 */
export type PreviewLink = 'connecting' | 'open' | 'unavailable' | 'unsupported';

/**
 * no_video: 중계가 이 카메라의 영상을 아직 못 받음(codec 없음), waiting_keyframe: codec 은 왔고 키프레임 대기,
 * playing: 그리는 중, decode_error: 디코더 오류(다음 키프레임에서 다시 시작).
 */
export type CameraPhase = 'no_video' | 'waiting_keyframe' | 'playing' | 'decode_error';

export interface CameraView {
  phase: CameraPhase;
  width?: number;
  height?: number;
  /** 마지막으로 그린 프레임의 추정 촬영 시각(UTC ms). */
  lastCaptureMs?: number;
}

interface CctvState {
  link: PreviewLink;
  /** 중계가 hello 로 알린 카메라(설정 순서). 연결 전에는 비어 있다. */
  cameras: string[];
  views: Record<string, CameraView>;
  /** 이번 화면에서 한 번이라도 중계에 붙었는지. 끊겨도 마지막 화면을 안내로 덮지 않는다. */
  everOpened: boolean;
  setLink: (link: PreviewLink) => void;
  hello: (cameras: string[]) => void;
  codec: (cameraId: string, width: number, height: number) => void;
  waitKeyframe: (cameraId: string) => void;
  played: (cameraId: string, captureMs: number) => void;
  decodeError: (cameraId: string) => void;
  reset: () => void;
}

const initial = { link: 'connecting' as PreviewLink, cameras: [], views: {}, everOpened: false };

const patch = (
  views: Record<string, CameraView>,
  cameraId: string,
  next: Partial<CameraView>,
): Record<string, CameraView> => ({
  ...views,
  [cameraId]: { ...(views[cameraId] ?? { phase: 'no_video' }), ...next },
});

export const useCctvStore = create<CctvState>()((set) => ({
  ...initial,
  setLink: (link) => set((s) => ({ link, everOpened: s.everOpened || link === 'open' })),
  hello: (cameras) =>
    set((s) => ({
      cameras,
      views: Object.fromEntries(
        cameras.map((id) => [id, s.views[id] ?? ({ phase: 'no_video' } as CameraView)]),
      ),
    })),
  codec: (cameraId, width, height) =>
    set((s) => ({ views: patch(s.views, cameraId, { phase: 'waiting_keyframe', width, height }) })),
  waitKeyframe: (cameraId) =>
    set((s) =>
      s.views[cameraId]?.phase === 'waiting_keyframe'
        ? s
        : { views: patch(s.views, cameraId, { phase: 'waiting_keyframe' }) },
    ),
  played: (cameraId, captureMs) =>
    set((s) => ({
      views: patch(s.views, cameraId, { phase: 'playing', lastCaptureMs: captureMs }),
    })),
  decodeError: (cameraId) =>
    set((s) => ({ views: patch(s.views, cameraId, { phase: 'decode_error' }) })),
  reset: () => set(initial),
}));
