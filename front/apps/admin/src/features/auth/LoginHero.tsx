import { useMediaQuery } from '@/features/auth/useMediaQuery';
import { cn } from '@simai/ui/lib/utils';
import { Box, ShieldAlert, Watch, type LucideIcon } from 'lucide-react';
import { useState } from 'react';

// 마스코트 장면 영상(-622): 원형 그리드 바닥 하나 위에 홀로그램 건물(왼쪽)과 너구리(오른쪽)가 함께 선 9:16(720×1280),
// 7초 반복, 소리 없음. 생성 영상(8초)의 끝 1초를 첫 1초에 겹쳐 녹여 이음새 없이 반복되게 다시 만들었다.
// 카메라는 고정이라 영상 시간으로 기능 칩 강조를 맞춘다. 포스터는 첫 장면이다.
// 생성 영상의 움직임이 조금 빨라 0.8배로 튼다(7초 반복 → 약 8.75초). 칩 강조 구간은 영상 안 시간(currentTime)이라 그대로 맞다.
export const MASCOT_PLAYBACK_RATE = 0.8;

export const MASCOT_MEDIA = {
  webm: '/media/mascot.webm',
  mp4: '/media/mascot.mp4',
  poster: '/media/mascot-poster.webp',
} as const;

interface SceneFeature {
  id: 'twin' | 'risk' | 'watch';
  icon: LucideIcon;
  label: string;
  /** 이 칩을 강조하는 영상 시간 구간(초). 7초 반복 영상에서 잰 값: 스캔 상승 0.2~2.3, 층 주황 점등 2.8~3.15(이어 스캔 하강 ~3.9), 손목 시계 빛 4.35~4.95. */
  during: readonly [number, number];
}

// 기능은 장면 아래(바닥 원 밑)에 나란히 놓은 작은 칩으로 보이고, 영상의 해당 장면 동안만 밝게 강조한다(나머지는 어둡게). 예전에는 장면 속 대상 옆에 띄우고 연결선을 그었는데 화면 위아래로
// 흩어져 시선이 크게 움직이고 장면이 복잡해 보였다(-622 피드백). 수치·"정상 운영 중" 같은 상태처럼 보이는 표시는 넣지 않는다.
const FEATURES: readonly SceneFeature[] = [
  { id: 'twin', icon: Box, label: '3D 현장 재현', during: [0, 2.6] },
  { id: 'risk', icon: ShieldAlert, label: 'AI 위험 감지', during: [2.6, 4.1] },
  { id: 'watch', icon: Watch, label: '워치 알림 연동', during: [4.1, 5.6] },
];

/** 영상 재생 시간에 맞는 기능 카드(없으면 null). */
export function activeFeatureAt(seconds: number): SceneFeature['id'] | null {
  return FEATURES.find(({ during: [from, to] }) => seconds >= from && seconds < to)?.id ?? null;
}

// 영상 가장자리는 바탕으로 녹여 사각 경계가 보이지 않게 한다. 좌우 흐림은 짧게(10%) 둔다. 영상 바탕이 페이지 바탕보다 조금 밝아
// 장면 뒤 빛 번짐이 둘레를 받친다. 위·아래 흐림은 대부분 화면 밖으로 잘리는 부분이다.
// 마스크 반복을 끈다(기본 repeat 이면 소수점 폭 가장자리에 다음 타일이 한 줄 비친다, -620).
const MEDIA_CLASS =
  'absolute inset-0 size-full object-cover mask-no-repeat mask-l-from-90% mask-t-from-90% mask-b-from-85% mask-r-from-90%';

function SceneFeatures({ active }: { active: SceneFeature['id'] | null }) {
  return (
    <ul
      className="absolute top-[82.5%] left-1/2 z-10 flex -translate-x-1/2 gap-2"
      data-testid="login-hero-features"
    >
      {FEATURES.map(({ id, icon: Icon, label }) => (
        <li
          key={id}
          data-active={active === id}
          className="group flex items-center gap-1.5 rounded-full border border-white/8 bg-login-hero/50 px-3.5 py-1.5 text-[13.5px] font-medium whitespace-nowrap text-white/45 backdrop-blur-md transition-[border-color,background-color,color,box-shadow] duration-500 data-[active=true]:border-sky-300/80 data-[active=true]:bg-sky-500/20 data-[active=true]:text-white data-[active=true]:shadow-[0_0_22px_rgba(56,189,248,0.5)]"
        >
          <Icon
            className="size-[15px] text-sky-300/45 transition-colors duration-500 group-data-[active=true]:text-sky-200"
            aria-hidden="true"
          />
          {label}
        </li>
      ))}
    </ul>
  );
}

/** 두 줄 헤드라인. 왼쪽 폼 묶음에서 로고 바로 아래에 둔다(-622). 설명 문장은 장면의 기능 카드가 대신한다. */
export function LoginHeadline() {
  return (
    <p className="text-[30px] leading-[1.2] font-bold tracking-[-0.8px] break-keep xl:text-[36px]">
      <span className="block text-white">현장은 입체적으로.</span>
      <span className="block bg-linear-to-r from-[#45D9F7] to-[#3498FA] bg-clip-text text-transparent">
        위험은 한발 먼저.
      </span>
    </p>
  );
}

/**
 * 로그인 화면 오른쪽 장면. 마스코트 장면 영상과 장면 속 대상에 연결된 기능 카드 세 개를 보인다(-622).
 * 칠·테두리·둥근 틀이 없다. 바탕과 빛 번짐은 페이지(LoginPage main)가 깔아 왼쪽 폼과 한 장면으로 이어지고, 넘친 영상은 페이지가 자른다.
 * dark 를 붙여 다크 토큰을 쓴다. 움직임 줄이기 설정이면 영상 대신 첫 장면만 보이고 강조도 멈춘다.
 */
export function LoginHero({ className }: { className?: string }) {
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)');
  const [active, setActive] = useState<SceneFeature['id'] | null>(null);

  return (
    // 폭은 영상 높이 122svh 에 맞춘 장면 폭(69svh = 122svh × 9/16)이고, 좁은 화면에서는 56vw 로 줄인다. 폼 묶음과 붙어 화면 가운데에 모인다(LoginPage).
    <aside
      className={cn(
        'dark relative isolate h-svh w-[min(69svh,56vw)] shrink-0 text-foreground',
        className,
      )}
    >
      {/* 장면 뒤 빛 번짐. 영상 가장자리가 흐려지는 자리에서 영상 바탕과 페이지 바탕 사이를 이어 준다. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -inset-x-[25%] inset-y-0 -z-10 bg-[radial-gradient(closest-side,rgba(37,99,235,0.2),rgba(37,99,235,0.06)_70%,transparent)]"
      />
      {/* 건물 꼭대기~바닥 원(영상 세로 21~80%, 가운데 50.5%)이 화면 높이의 약 72%를 차지하게 키우고(높이 122svh), 그 가운데를 화면 44%
          높이에 둔다. 조금 위로 올려 바닥 원과 아래 기능 칩 사이에 간격을 남긴다. */}
      <div
        className="absolute top-[44%] left-0 aspect-[9/16] w-full -translate-y-[50.5%]"
        data-testid="login-hero-media"
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
            // 재생 속도는 소스를 읽을 때마다 기본값으로 돌아가 메타데이터를 읽은 뒤 다시 정한다.
            onLoadedMetadata={(event) => {
              event.currentTarget.defaultPlaybackRate = MASCOT_PLAYBACK_RATE;
              event.currentTarget.playbackRate = MASCOT_PLAYBACK_RATE;
            }}
            onTimeUpdate={(event) => setActive(activeFeatureAt(event.currentTarget.currentTime))}
          >
            <source src={MASCOT_MEDIA.webm} type="video/webm" />
            <source src={MASCOT_MEDIA.mp4} type="video/mp4" />
          </video>
        )}
        {/* 칩은 영상 기준으로 바닥 원(영상 세로 ~79%) 바로 아래, 원 중심(영상 가로 가운데)에 맞춰 둔다. 화면 크기와 관계없이 원과 칩 사이 간격(약 3%)이 같다. */}
        <SceneFeatures active={reducedMotion ? null : active} />
      </div>
    </aside>
  );
}
