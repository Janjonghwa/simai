import { targetKey } from '@/features/viewer3d/target';
import type { ViewerTarget } from '@/features/viewer3d/types';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import type { ThreeEvent } from '@react-three/fiber';
import { useCallback, useEffect, type ReactNode } from 'react';

interface SelectableProps {
  target: ViewerTarget;
  children: ReactNode;
  /**
   * 이 대상보다 먼저 골라야 할 대상이 광선에 있으면 그것을 돌려준다(투명 구역이 안·뒤의 사람을 가리지 않게).
   * 없으면 null 이고 이 대상을 고른다.
   */
  pickInstead?: (event: ThreeEvent<MouseEvent>) => ViewerTarget | null;
}

/** 장면 객체를 선택·초점 대상으로 올린다. 사람·구역·OBB 레이어가 이것으로 감싼다. */
export function Selectable({ target, children, pickInstead }: SelectableProps) {
  const store = useViewerStore();
  const { type, id } = target;

  useEffect(() => {
    const registered = { type, id };
    store.getState().registerTarget(registered);
    return () => store.getState().unregisterTarget(registered);
  }, [store, type, id]);

  const handleClick = useCallback(
    (event: ThreeEvent<MouseEvent>) => {
      event.stopPropagation();
      store.getState().select(pickInstead?.(event) ?? { type, id });
    },
    [store, type, id, pickInstead],
  );

  return (
    <group name={targetKey({ type, id })} onClick={handleClick}>
      {children}
    </group>
  );
}
