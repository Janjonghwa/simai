import { useMediaQuery } from '@/features/auth/useMediaQuery';
import { BrandMark, Wordmark } from '@/features/layout/BrandMark';
import { cn } from '@simai/ui/lib/utils';
import { Cctv, ShieldAlert, Watch, type LucideIcon } from 'lucide-react';

// 마스코트 영상은 public/media 에 둔다(원본 720×1280 10초를 540 px 폭, 소리 없이 줄였다).
export const MASCOT_MEDIA = {
  webm: '/media/mascot.webm',
  mp4: '/media/mascot.mp4',
  poster: '/media/mascot-poster.webp',
} as const;

const FEATURES: { icon: LucideIcon; title: string; detail: string }[] = [
  {
    icon: Cctv,
    title: '3D 디지털 트윈',
    detail: '고정 카메라 4대로 현장을 미터 단위 3D로 옮깁니다',
  },
  {
    icon: ShieldAlert,
    title: '실시간 위험 판정',
    detail: '작업자 위치와 구역 규칙으로 위험을 바로 가립니다',
  },
  {
    icon: Watch,
    title: '스마트워치 즉시 알림',
    detail: '위험이 생기면 작업자 워치에 진동으로 바로 알립니다',
  },
];

/*
 * 영상은 세로(9:16)라 카드를 덮으면 마스코트가 지나치게 커진다. 카드 높이의 80% 로 가운데 세우고 둘레를 타원으로 흐리게 지우며,
 * 그 뒤에 첫 장면을 크게 흐려 깔아 카드 전체가 같은 우주 배경으로 이어지게 한다.
 */
const MEDIA_CLASS =
  'absolute top-0 left-1/2 aspect-[9/16] h-[80%] -translate-x-1/2 object-cover [mask-image:radial-gradient(ellipse_50%_56%_at_50%_48%,black_68%,transparent)]';

/**
 * 로그인 화면 오른쪽 카드. 마스코트 영상(장식)과 로고·기능 세 줄을 보인다.
 * 카드는 화이트 모드에서도 남색이라 dark 를 붙여 다크 토큰을 쓴다. 움직임 줄이기 설정이면 영상 대신 첫 장면만 보인다.
 */
export function LoginHero({ className }: { className?: string }) {
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)');

  return (
    <aside
      className={cn(
        'dark relative isolate flex min-h-0 flex-col justify-end overflow-hidden rounded-[28px] bg-surface-3 text-foreground shadow-float',
        className,
      )}
    >
      <img
        src={MASCOT_MEDIA.poster}
        alt=""
        aria-hidden="true"
        className="absolute inset-0 size-full scale-125 object-cover opacity-40 blur-3xl"
      />
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
      {/* 아래 글자가 영상 위에서도 읽히게 카드 아래쪽을 바탕색으로 덮는다. */}
      <div
        aria-hidden="true"
        className="absolute inset-x-0 bottom-0 h-[55%] bg-linear-to-t from-background from-40% via-background/70 to-transparent"
      />
      <div className="relative flex flex-col gap-7 p-10 xl:p-12">
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2.5">
            <BrandMark size={30} />
            <Wordmark />
          </div>
          <p className="text-[22px] leading-snug font-semibold tracking-[-0.3px] break-keep">
            현장을 3D로 보고, 위험은 먼저 알립니다.
          </p>
        </div>
        <ul className="grid gap-4">
          {FEATURES.map(({ icon: Icon, title, detail }) => (
            <li key={title} className="flex items-center gap-3.5">
              <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-primary/15 text-point-soft ring-1 ring-primary/30">
                <Icon className="size-[18px]" aria-hidden="true" />
              </span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="text-[15px] font-semibold">{title}</span>
                <span className="text-[13px] break-keep text-muted-foreground">{detail}</span>
              </span>
            </li>
          ))}
        </ul>
      </div>
    </aside>
  );
}
