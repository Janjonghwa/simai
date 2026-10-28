import { targetKey } from '@/features/viewer3d/target';
import type { ViewerTarget } from '@/features/viewer3d/types';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import type { ThreeEvent } from '@react-three/fiber';
import { useCallback, useEffect, useRef, type ReactNode } from 'react';

/** 같은 대상을 연속 클릭했을 때 더블클릭으로 인정하는 최대 간격(ms). */
const DOUBLE_CLICK_MS = 400;

interface SelectableProps {
  target: ViewerTarget;
  children: ReactNode;
  /**
   * 이 대상보다 먼저 골라야 할 대상이 광선에 있으면 그것을 돌려준다(투명 구역이 안·뒤의 사람을 가리지 않게).
   * 없으면 null 이고 이 대상을 고른다.
   */
  pickInstead?: (event: ThreeEvent<MouseEvent>) => ViewerTarget | null;
  /**
   * false 면 장면 대상으로는 올리되(명령 select·focus 가능) 클릭을 받지 않는다(-234 레이어 visible).
   * 처리기가 없는 객체는 R3F 광선 검사에서 빠지므로 뒤의 대상·빈 곳 클릭을 가리지 않는다.
   */
  interactive?: boolean;
}

/** 장면 객체를 선택·초점 대상으로 올린다. 사람·구역·OBB 레이어가 이것으로 감싼다. */
export function Selectable({ target, children, pickInstead, interactive = true }: SelectableProps) {
  const store = useViewerStore();
  const { type, id } = target;
  const lastClickRef = useRef<{ key: string; at: number } | null>(null);

  useEffect(() => {
    const registered = { type, id };
    store.getState().registerTarget(registered);
    return () => store.getState().unregisterTarget(registered);
  }, [store, type, id]);

  const handleClick = useCallback(
    (event: ThreeEvent<MouseEvent>) => {
      // 끈 동작(카메라 회전·손잡이 끌기)은 클릭이 아니다. R3F 가 빈 곳 클릭(onPointerMissed)에 쓰는 기준(2px)과 같다.
      if (event.delta > 2) return;
      event.stopPropagation();
      const picked = pickInstead?.(event) ?? { type, id };
      const key = targetKey(picked);
      const now = Date.now();
      const prev = lastClickRef.current;
      store.getState().select(picked);
      if (
        (event.nativeEvent && event.nativeEvent.detail >= 2) ||
        (prev !== null && prev.key === key && now - prev.at <= DOUBLE_CLICK_MS)
      ) {
        lastClickRef.current = null;
        store.getState().activate(picked);
        return;
      }
      lastClickRef.current = { key, at: now };
    },
    [store, type, id, pickInstead],
  );

  return (
    <group name={targetKey({ type, id })} onClick={interactive ? handleClick : undefined}>
      {children}
    </group>
  );
}
