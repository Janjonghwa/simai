import {
  createSpatialDataStore,
  type SpatialDataStore,
} from '@/features/viewer3d/data/spatialData.store';
import { SpatialDataProvider } from '@/features/viewer3d/data/SpatialDataProvider';
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
  /** 데이터 Store 를 바깥에서 읽어야 할 때(개발 진단·시험)만 넘긴다. 없으면 인스턴스마다 하나 만든다. */
  dataStore?: SpatialDataStore;
}

/** 뷰어 상태·명령·알림과 데이터 입력을 묶는다. 인스턴스가 살아 있는 동안 store 와 Canvas 는 하나다. */
export function ViewerShell({
  ref,
  initialMode = 'LIVE',
  onSelectionChange,
  onStatusChange,
  source,
  className,
  children,
  dataStore,
}: ViewerShellProps) {
  const [store] = useState(() => createViewerStore(initialMode));
  // 처음 받은 dataStore 를 계속 쓴다. 바꿔 넘겨도 장면의 구독을 옮기지 않는다.
  const [data] = useState(() => dataStore ?? createSpatialDataStore());
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
      {/* 렌더 오류로 Canvas 가 내려가도 데이터 구독은 이어 간다(뷰어는 경보 경로가 아니지만 상태는 잃지 않는다). */}
      <SpatialDataProvider store={data} source={source}>
        <ViewerErrorBoundary onError={handleRenderError}>
          <ViewerStoreContext value={store}>
            <ViewerCanvas>{children}</ViewerCanvas>
          </ViewerStoreContext>
        </ViewerErrorBoundary>
      </SpatialDataProvider>
    </div>
  );
}
