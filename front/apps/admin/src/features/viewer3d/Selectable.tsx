import { targetKey } from '@/features/viewer3d/target';
import type { ViewerTarget } from '@/features/viewer3d/types';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import type { ThreeEvent } from '@react-three/fiber';
import { useCallback, useEffect, type ReactNode } from 'react';

interface SelectableProps {
  target: ViewerTarget;
  children: ReactNode;
}

/** 장면 객체를 선택·초점 대상으로 올린다. 사람·구역·OBB 레이어가 이것으로 감싼다. */
export function Selectable({ target, children }: SelectableProps) {
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
      store.getState().select({ type, id });
    },
    [store, type, id],
  );

  return (
    <group name={targetKey({ type, id })} onClick={handleClick}>
      {children}
    </group>
  );
}
