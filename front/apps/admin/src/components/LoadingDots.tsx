import { useMediaQuery } from '@/features/auth/useMediaQuery';
import { cn } from '@simai/ui/lib/utils';
import { useEffect, useState } from 'react';

// 기다리는 중 표시: 글 뒤에 점이 . → .. → ... 로 되풀이해 늘어난다. 움직임 줄이기 설정이면 점 세 개를 멈춰 둔다.
// 점은 장식이라 보조 기술에서 숨긴다(상태 영역 안에서 점이 바뀔 때마다 다시 읽지 않게). 글은 따로 감싸 그대로 찾을 수 있다.

/** 점 하나가 늘어나는 간격(ms). */
export const LOADING_DOT_STEP_MS = 400;
const MAX_DOTS = 3;

export function LoadingDots({ label, className }: { label: string; className?: string }) {
  const reduced = useMediaQuery('(prefers-reduced-motion: reduce)');
  const [count, setCount] = useState(1);
  useEffect(() => {
    if (reduced) return undefined;
    const timer = setInterval(
      () => setCount((current) => (current % MAX_DOTS) + 1),
      LOADING_DOT_STEP_MS,
    );
    return () => clearInterval(timer);
  }, [reduced]);
  return (
    <span className={cn('inline-flex items-baseline', className)}>
      <span>{label}</span>
      {/* 점 자리 폭을 고정해 점이 늘 때 글이 흔들리지 않게 한다 */}
      <span aria-hidden data-slot="loading-dots" className="inline-block w-[1.5em] text-left">
        {'.'.repeat(reduced ? MAX_DOTS : count)}
      </span>
    </span>
  );
}
