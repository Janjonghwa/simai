import type { CameraGuideView } from '@/features/setup/cameraGuides';

// 서버가 계산한 방향과 조정 크기를 영상 위의 작은 안내로 보여 준다.
const DIRECTION = {
  LEFT: { arrow: '←', word: '왼쪽으로' },
  RIGHT: { arrow: '→', word: '오른쪽으로' },
  UP: { arrow: '↑', word: '위로' },
  DOWN: { arrow: '↓', word: '아래로' },
} as const;

/** 영상이 그려지는 사각형을 잡는다. */
export function videoBox(aspect: number) {
  return { width: `min(100cqw, ${aspect} * 100cqh)`, aspectRatio: aspect };
}

/** 한 카메라 영상 위의 방향 안내. */
export function CameraCue({ guide, aspect }: { guide: CameraGuideView; aspect: number }) {
  if (guide.direction === 'NONE') return null;
  const direction = DIRECTION[guide.direction];
  const amount = guide.magnitude === 'MORE' ? '더' : guide.magnitude === 'SMALL' ? '조금' : '';
  return (
    <div
      aria-hidden
      className="[container-type:size] pointer-events-none absolute inset-0 grid place-items-center"
    >
      <div className="relative" style={videoBox(aspect)}>
        <span
          data-testid="camera-cue-arrow"
          data-direction={guide.direction}
          className="absolute top-2 right-2 rounded-md bg-card/90 px-2 py-1 text-xs font-semibold text-delay"
        >
          {direction.arrow} {direction.word} {amount}
        </span>
      </div>
    </div>
  );
}
