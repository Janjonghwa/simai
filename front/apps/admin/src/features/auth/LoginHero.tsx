import { useMediaQuery } from '@/features/auth/useMediaQuery';
import { cn } from '@simai/ui/lib/utils';
import { Cctv, ShieldAlert, Watch, type LucideIcon } from 'lucide-react';

// 마스코트 영상은 public/media 에 둔다(원본 720×1280 10초를 540 px 폭, 소리 없이 줄였다).
export const MASCOT_MEDIA = {
  webm: '/media/mascot.webm',
  mp4: '/media/mascot.mp4',
  poster: '/media/mascot-poster.webp',
} as const;

// 글자 영역이 마스코트를 덜 가리게 기능은 한 줄씩만 적는다.
const FEATURES: { icon: LucideIcon; text: string }[] = [
  { icon: Cctv, text: '고정 카메라 4대로 3D 디지털 트윈' },
  { icon: ShieldAlert, text: '실시간 위험 판정' },
  { icon: Watch, text: '스마트워치로 즉시 알림' },
];

// 영상(포스터)은 카드를 꽉 채운다. 세로 영상이라 위아래가 잘리는데, 안전모 위를 덜 남기고 몸통이 글자 위로 보이게 45% 에 맞춘다.
const MEDIA_CLASS = 'absolute inset-0 size-full object-cover object-[50%_45%]';

/**
 * 로그인 화면 오른쪽 카드. 마스코트 영상(장식)과 로고·기능 세 줄을 보인다.
 * 카드는 화이트 모드에서도 남색이라 dark 를 붙여 다크 토큰을 쓴다. 움직임 줄이기 설정이면 영상 대신 첫 장면만 보인다.
 */
export function LoginHero({ className }: { className?: string }) {
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)');

  return (
    <aside
      className={cn(
        'dark relative isolate flex min-h-0 flex-col justify-end overflow-hidden rounded-[28px] bg-login-hero text-foreground shadow-float',
        className,
      )}
    >
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
      {/* 아래 글자가 영상 위에서도 읽히게 글자 영역만 카드 바탕색(영상 아래 가장자리 색)으로 부드럽게 덮는다. */}
      <div
        aria-hidden="true"
        className="absolute inset-x-0 bottom-0 h-[46%] bg-linear-to-t from-login-hero from-40% via-login-hero/75 via-70% to-transparent"
      />
      <div className="relative flex flex-col gap-6 p-10 xl:p-12">
        <div className="flex flex-col gap-3">
          <p className="text-[22px] leading-snug font-semibold tracking-[-0.3px] break-keep">
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
    </aside>
  );
}
