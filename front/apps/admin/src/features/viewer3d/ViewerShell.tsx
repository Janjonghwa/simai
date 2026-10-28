import {
  createSpatialDataStore,
  type SpatialDataStore,
} from '@/features/viewer3d/data/spatialData.store';
import { SpatialDataProvider } from '@/features/viewer3d/data/SpatialDataProvider';
import {
  createEditStore,
  objectPreviewOf,
  previewOf,
} from '@/features/viewer3d/editing/editSession.store';
import { EditOverlayContext, EditStoreContext } from '@/features/viewer3d/editing/EditStoreContext';
import type {
  ViewerEditPreview,
  ViewerEditResult,
  ViewerObjectEditPreview,
  ViewerObjectEditResult,
} from '@/features/viewer3d/editing/editTypes';
import { PersonModelContext } from '@/features/viewer3d/people/PersonLayer';
import {
  sameSceneStatus,
  sceneStatusOf,
  type ViewerSceneStatus,
} from '@/features/viewer3d/scene/sceneContract';
import {
  createSceneStore,
  loadGltfScene,
  SceneLoaderContext,
  SceneStoreContext,
  sceneVersionsOf,
  type SceneAssetLoader,
} from '@/features/viewer3d/scene/sceneStore';
import type {
  Viewer3DProps,
  ViewerSelectRequest,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d/types';
import { createViewerStore, toStatus } from '@/features/viewer3d/viewer3d.store';
import { ViewerCanvas } from '@/features/viewer3d/ViewerCanvas';
import { ViewerErrorBoundary } from '@/features/viewer3d/ViewerErrorBoundary';
import { ViewerStoreContext } from '@/features/viewer3d/ViewerStoreContext';
import { ZoneApplyContext, type ZoneApplyState } from '@/features/viewer3d/zones/zonePresentation';
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
  /**
   * 구역 적용 상태 표시. 적용 증거 계약(BE -120·FE -189)이 없어 기본은 UNVERIFIED 이고, DEMO 값은 개발 화면 시험용이다.
   * 바꿔도 Canvas·장면은 그대로이고 구역 모양만 바뀐다.
   */
  zoneApplyState?: ZoneApplyState;
  /** 배경 glTF 읽기 함수. 시험만 바꿔 끼운다. 없으면 GLTFLoader. */
  sceneLoader?: SceneAssetLoader;
}

/** 뷰어 상태·명령·알림과 데이터 입력을 묶는다. 인스턴스가 살아 있는 동안 store 와 Canvas 는 하나다. */
export function ViewerShell({
  ref,
  initialMode = 'LIVE',
  onSelectionChange,
  onTargetDoubleClick,
  onStatusChange,
  onSelectRequestChange,
  onEditPreview,
  onEditResult,
  onObjectEditPreview,
  onObjectEditResult,
  editDraftOverlay = null,
  layers,
  source,
  className,
  children,
  dataStore,
  personModelUrl = null,
  zoneApplyState = 'UNVERIFIED',
  scene = null,
  onSceneStatusChange,
  sceneLoader = loadGltfScene,
}: ViewerShellProps) {
  const [store] = useState(() => {
    const created = createViewerStore(initialMode);
    // 첫 장면부터 레이어 값을 따른다(기본값으로 한 번 그렸다가 숨기지 않게).
    created.getState().setLayers(layers);
    return created;
  });
  // 처음 받은 dataStore 를 계속 쓴다. 바꿔 넘겨도 장면의 구독을 옮기지 않는다.
  const [data] = useState(() => dataStore ?? createSpatialDataStore());
  // 편집 세션(-195). 뷰어 Store·데이터 Store 를 읽기만 하고 원본을 바꾸지 않는다.
  const [edit] = useState(() => createEditStore(store, data));
  // 정적 배경 Scene(-59). 입력은 FE 가 받은 /api/scenes/current 결과 그대로다.
  const [sceneStore] = useState(createSceneStore);
  const mode = useStore(store, (s) => s.mode);
  const phase = useStore(store, (s) => s.phase);

  useImperativeHandle(ref, () => {
    const { requestSelect, clearSelection, focusOn, setMode } = store.getState();
    const {
      begin,
      cancel,
      finish,
      updateField,
      cancelObject,
      finishObject,
      updateObjectField,
      createObject,
    } = edit.store.getState();
    return {
      select: (target, options) => requestSelect(target, options),
      clearSelection,
      focus: focusOn,
      setMode,
      beginEdit: (target, options) => begin(target, options),
      cancelEdit: () => cancel('USER'),
      finishEdit: finish,
      updateEditField: (change) => updateField(change.field, change.value),
      cancelObjectEdit: () => cancelObject('USER'),
      finishObjectEdit: finishObject,
      updateObjectEditField: (change) => updateObjectField(change.field, change.value),
      beginObjectCreate: createObject,
    };
  }, [store, edit]);

  const emitSelection = useEffectEvent((target: ViewerTarget | null) =>
    onSelectionChange?.(target),
  );
  const emitTargetDoubleClick = useEffectEvent((target: ViewerTarget) =>
    onTargetDoubleClick?.(target),
  );
  const emitSelectRequest = useEffectEvent((request: ViewerSelectRequest) =>
    onSelectRequestChange?.(request),
  );
  const emitStatus = useEffectEvent((status: ViewerStatus) => onStatusChange?.(status));
  const emitEditPreview = useEffectEvent((preview: ViewerEditPreview) => onEditPreview?.(preview));
  const emitEditResult = useEffectEvent((result: ViewerEditResult) => onEditResult?.(result));
  const emitObjectEditPreview = useEffectEvent((preview: ViewerObjectEditPreview) =>
    onObjectEditPreview?.(preview),
  );
  const emitObjectEditResult = useEffectEvent((result: ViewerObjectEditResult) =>
    onObjectEditResult?.(result),
  );
  const emitSceneStatus = useEffectEvent((status: ViewerSceneStatus) =>
    onSceneStatusChange?.(status),
  );

  // 레이어 값이 같으면 Store 를 바꾸지 않는다(렌더마다 새 객체를 넘겨도 다시 그리지 않는다).
  useEffect(() => {
    store.getState().setLayers(layers);
  }, [store, layers]);

  // 기다리던 선택은 뷰어가 내려갈 때 UNMOUNTED 로 끝내고 타이머를 푼다.
  useEffect(() => () => store.getState().dispose(), [store]);

  useEffect(() => {
    sceneStore.getState().setInput(scene);
  }, [sceneStore, scene]);

  // 배경 상태는 입력·읽기 단계·World·Health 버전이 바뀔 때만 알린다(10 Hz World 마다 알리지 않는다).
  useEffect(() => {
    let last: ViewerSceneStatus | null = null;
    const update = () => {
      const { input, load } = sceneStore.getState();
      const next = sceneStatusOf(input, load, sceneVersionsOf(data.getState()));
      if (last && sameSceneStatus(last, next)) return;
      last = next;
      emitSceneStatus(next);
    };
    update();
    const offScene = sceneStore.subscribe(update);
    const offData = data.subscribe((state, prev) => {
      if (state.world?.sceneVersion !== prev.world?.sceneVersion) update();
      else if (state.world?.calibrationVersion !== prev.world?.calibrationVersion) update();
      else if (state.health !== prev.health) update();
    });
    return () => {
      offScene();
      offData();
    };
  }, [sceneStore, data]);

  useEffect(() => {
    let last = toStatus(store.getState());
    emitStatus(last);
    return store.subscribe((state, prev) => {
      if (state.selected !== prev.selected) emitSelection(state.selected);
      if (state.selectRequest && state.selectRequest !== prev.selectRequest) {
        emitSelectRequest(state.selectRequest);
      }
      if (state.activated && state.activated !== prev.activated) {
        emitTargetDoubleClick(state.activated.target);
      }
      if (state.phase !== last.phase || state.mode !== last.mode || state.error !== last.error) {
        last = toStatus(state);
        emitStatus(last);
      }
    });
  }, [store]);

  // 편집 세션을 선택·모드·원본 변경에 묶고, 뷰어가 내려가면 진행 중인 편집을 UNMOUNTED 로 취소한다.
  useEffect(() => edit.connect(), [edit]);

  // 초안은 시작·손잡이를 놓을 때만 알린다(끄는 도중 매 움직임마다 FE 를 다시 그리게 하지 않는다).
  useEffect(
    () =>
      edit.store.subscribe((state, prev) => {
        if (state.lastResult && state.lastResult !== prev.lastResult)
          emitEditResult(state.lastResult);
        if (state.lastObjectResult && state.lastObjectResult !== prev.lastObjectResult)
          emitObjectEditResult(state.lastObjectResult);
        const object = state.objectSession;
        const objectBefore = prev.objectSession;
        if (
          object &&
          !object.dragging &&
          (!objectBefore ||
            objectBefore.target !== object.target ||
            objectBefore.draft !== object.draft ||
            objectBefore.dragging)
        ) {
          emitObjectEditPreview(objectPreviewOf(object));
        }
        const now = state.session;
        const before = prev.session;
        if (!now || now.dragging) return;
        if (
          !before ||
          before.target !== now.target ||
          before.draft !== now.draft ||
          before.dragging
        ) {
          emitEditPreview(previewOf(now));
        }
      }),
    [edit],
  );

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
            <EditStoreContext value={edit.store}>
              <EditOverlayContext value={editDraftOverlay}>
                <PersonModelContext value={personModelUrl}>
                  <ZoneApplyContext value={zoneApplyState}>
                    <SceneStoreContext value={sceneStore}>
                      <SceneLoaderContext value={sceneLoader}>
                        <ViewerCanvas>{children}</ViewerCanvas>
                      </SceneLoaderContext>
                    </SceneStoreContext>
                  </ZoneApplyContext>
                </PersonModelContext>
              </EditOverlayContext>
            </EditStoreContext>
          </ViewerStoreContext>
        </ViewerErrorBoundary>
      </SpatialDataProvider>
    </div>
  );
}
