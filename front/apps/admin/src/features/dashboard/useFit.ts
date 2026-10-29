import { fitCount } from '@/features/dashboard/dashboard';
import { useEffect, useRef, useState, type RefObject } from 'react';

/**
 * 카드 높이에 맞춰 목록을 2~max 줄만 보인다(관제 데모와 같다). 줄은 모두 그려 두고 잰다.
 * 넘치는 줄은 자리를 둔 채 감추므로(invisible) 크기가 바뀌어도 다시 잴 수 있다.
 * key 는 그린 줄 목록이다. 줄이 바뀌면 새 줄도 지켜본다.
 */
export function useFit<T extends HTMLElement>(
  max: number,
  key: string,
): [RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [count, setCount] = useState(max);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const update = () => {
      const bottoms = Array.from(el.children, (child) =>
        child instanceof HTMLElement ? child.offsetTop + child.offsetHeight : 0,
      );
      setCount(fitCount(bottoms, el.clientHeight));
    };
    const observer = new ResizeObserver(update);
    observer.observe(el);
    for (const child of el.children) observer.observe(child);
    return () => observer.disconnect();
  }, [key]);
  return [ref, count];
}
