import { useMediaQuery } from '@/features/auth/useMediaQuery';
import { cn } from '@simai/ui/lib/utils';
import { Cctv, ShieldAlert, Watch, type LucideIcon } from 'lucide-react';

// 마스코트 영상은 public/media 에 둔다. 원본(720×1280, 8초)에서 위쪽 빈 하늘과 아래 바닥을 잘라 안전모 위부터 받침대 그림자까지(세로 300~1180 px)만
// 남기고 540×660, 소리 없이 줄였다. 포스터는 첫 장면이다.
export const MASCOT_MEDIA = {
  webm: '/media/mascot.webm',
  mp4: '/media/mascot.mp4',
  poster: '/media/mascot-poster.webp',
} as const;

// 기능은 한 줄씩만 적는다.
const FEATURES: { icon: LucideIcon; text: string }[] = [
  { icon: Cctv, text: '고정 카메라 4대로 3D 디지털 트윈' },
  { icon: ShieldAlert, text: '실시간 위험 판정' },
  { icon: Watch, text: '스마트워치로 즉시 알림' },
];

// 영상(포스터)은 글자 아래 남은 칸에 키우지 않고 맞춘다(contain). 칸 높이를 다 쓰고 폭은 영상 비율만큼이라 마스코트가 안전모부터
// 받침대까지 잘리지 않는다. 영상이 카드 높이를 밀어 올리지 않게 칸 안에 띄워(absolute) 바닥 가운데에 둔다. 남는 양옆과 위아래 가장자리는 카드 바탕색으로 서서히 사라지게 가려 띠가 보이지 않게 한다.
// 원본에서 받침대·꼬리가 오른쪽 끝에 닿아 있어(다시 잘라도 살릴 수 없음) 오른쪽은 22% 에 걸쳐 흐린다. 10% 면 1024 폭에서 받침대가 칼로 자른 듯 끊겼다(-591).
const MEDIA_CLASS =
  'absolute inset-x-0 bottom-0 mx-auto aspect-[540/660] h-full max-w-full object-contain mask-t-from-88% mask-r-from-78% mask-b-from-80% mask-l-from-72%';

/**
 * 로그인 화면 오른쪽 카드. 글귀·기능 세 줄(위)과 마스코트 영상(장식, 아래)을 보인다.
 * 카드는 화이트 모드에서도 남색이라 dark 를 붙여 다크 토큰을 쓴다. 움직임 줄이기 설정이면 영상 대신 첫 장면만 보인다.
 */
export function LoginHero({ className }: { className?: string }) {
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)');

  return (
    <aside
      className={cn(
        'dark relative isolate flex min-h-0 flex-col overflow-hidden rounded-[28px] bg-login-hero text-foreground shadow-float',
        className,
      )}
    >
      <div className="flex shrink-0 flex-col gap-6 p-10 pb-2 xl:p-12 xl:pb-4">
        <div className="flex flex-col gap-3">
          <p className="text-[22px] leading-snug font-semibold tracking-[-0.3px] text-balance break-keep">
            현장을 3D로 보고, 위험은 먼저 알립니다.
          </p>
        </div>
        <ul className="grid gap-3">
          {FEATURES.map(({ icon: Icon, text }) => (
            <li key={text} className="flex items-center gap-3">
              <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary/15 text-point-soft ring-1 ring-primary/30">
                <Icon className="size-[18px]" aria-hidden="true" />
              </span>
              <span className="text-[15px] font-medium break-keep">{text}</span>
            </li>
          ))}
        </ul>
      </div>
      {/* 글자는 위에 두고 영상은 그 아래 남은 칸 바닥에 붙인다. 글자가 마스코트를 덮지 않는다. */}
      <div className="relative min-h-0 flex-1" data-testid="login-hero-media">
        {reducedMotion ? (
          <img
            src={MASCOT_MEDIA.poster}
            alt=""
            aria-hidden="true"
            data-testid="login-hero-poster"
            className={MEDIA_CLASS}
          />
        ) : (
          <video
            aria-hidden="true"
            tabIndex={-1}
            autoPlay
            loop
            muted
            playsInline
            preload="auto"
            disablePictureInPicture
            poster={MASCOT_MEDIA.poster}
            className={MEDIA_CLASS}
          >
            <source src={MASCOT_MEDIA.webm} type="video/webm" />
            <source src={MASCOT_MEDIA.mp4} type="video/mp4" />
          </video>
        )}
      </div>
    </aside>
  );
}
