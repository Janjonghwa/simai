import type { Viewer3DProps, ViewerStatus, ViewerTarget } from '@/features/viewer3d/types';
import { createViewerStore, toStatus } from '@/features/viewer3d/viewer3d.store';
import { ViewerCanvas } from '@/features/viewer3d/ViewerCanvas';
import { ViewerErrorBoundary } from '@/features/viewer3d/ViewerErrorBoundary';
import { ViewerStoreContext } from '@/features/viewer3d/ViewerStoreContext';
import { cn } from '@simai/ui/lib/utils';
import {
  useCallback,
  useEffect,
  useEffectEvent,
  useImperativeHandle,
  useState,
  type ReactNode,
} from 'react';
import { useStore } from 'zustand';

export interface ViewerShellProps extends Viewer3DProps {
  /** 장면 레이어. viewer3d 안과 시험에서만 넘긴다. */
  children?: ReactNode;
}

/** 뷰어 상태·명령·알림을 묶는다. 인스턴스가 살아 있는 동안 store 와 Canvas 는 하나다. */
export function ViewerShell({
  ref,
  initialMode = 'LIVE',
  onSelectionChange,
  onStatusChange,
  className,
  children,
}: ViewerShellProps) {
  const [store] = useState(() => createViewerStore(initialMode));
  const mode = useStore(store, (s) => s.mode);
  const phase = useStore(store, (s) => s.phase);

  useImperativeHandle(ref, () => {
    const { select, clearSelection, focusOn, setMode } = store.getState();
    return { select, clearSelection, focus: focusOn, setMode };
  }, [store]);

  const emitSelection = useEffectEvent((target: ViewerTarget | null) =>
    onSelectionChange?.(target),
  );
  const emitStatus = useEffectEvent((status: ViewerStatus) => onStatusChange?.(status));

  useEffect(() => {
    let last = toStatus(store.getState());
    emitStatus(last);
    return store.subscribe((state, prev) => {
      if (state.selected !== prev.selected) emitSelection(state.selected);
      if (state.phase !== last.phase || state.mode !== last.mode || state.error !== last.error) {
        last = toStatus(state);
        emitStatus(last);
      }
    });
  }, [store]);

  const handleRenderError = useCallback(
    (error: Error) => store.getState().markError({ code: 'RENDER_FAILED', message: error.message }),
    [store],
  );

  return (
    <div
      className={cn('relative size-full', className)}
      data-viewer-mode={mode}
      data-viewer-phase={phase}
    >
      <ViewerErrorBoundary onError={handleRenderError}>
        <ViewerStoreContext value={store}>
          <ViewerCanvas>{children}</ViewerCanvas>
        </ViewerStoreContext>
      </ViewerErrorBoundary>
    </div>
  );
}
