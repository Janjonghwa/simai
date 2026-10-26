import { createSpatialDataStore } from '@/features/viewer3d/data/spatialData.store';
import { DataPanel } from '@/features/viewer3d/dev-harness/DataPanel';
import {
  devViewOf,
  readDevView,
  readDiagnosticsOpen,
  withDevView,
  type DevViewKey,
} from '@/features/viewer3d/dev-harness/devView';
import { DevViewSwitcher } from '@/features/viewer3d/dev-harness/DevViewSwitcher';
import { EditPanel, EditProbe } from '@/features/viewer3d/dev-harness/EditPanel';
import {
  createHarnessSource,
  DEV_XBOT_URL,
  readAvatar,
  readDataSource,
  readZoneApply,
  withDataSource,
  withZoneApply,
  type DataSourceKey,
} from '@/features/viewer3d/dev-harness/harnessSource';
import { DebugProbe, SpatialDiagnostics } from '@/features/viewer3d/dev-harness/MockDebug';
import { MockNav } from '@/features/viewer3d/dev-harness/MockNav';
import { MockOverlay, SelectionCard } from '@/features/viewer3d/dev-harness/MockOverlay';
import { MockScene, type SceneColors } from '@/features/viewer3d/dev-harness/MockScene';
import { MockTopBar } from '@/features/viewer3d/dev-harness/MockTopBar';
import type { MenuKey } from '@/features/viewer3d/dev-harness/mockUi';
import type { SpatialMockApi } from '@/features/viewer3d/dev-harness/mswHandlers';
import { useRoster } from '@/features/viewer3d/dev-harness/useRoster';
import type {
  ViewerEditPreview,
  ViewerEditResult,
  ViewerObjectEditPreview,
  ViewerObjectEditResult,
} from '@/features/viewer3d/editing/editTypes';
import type { Viewer3DHandle, ViewerStatus, ViewerTarget } from '@/features/viewer3d/types';
import { ViewerShell } from '@/features/viewer3d/ViewerShell';
import { WorldReference } from '@/features/viewer3d/WorldReference';
import type { ZoneApplyState } from '@/features/viewer3d/zones/zonePresentation';
import { useMemo, useRef, useState } from 'react';

// 3D 재질 색도 FE 토큰(packages/ui globals.css)에서 읽어 다크·화이트 모드를 따른다.
function readColors(): SceneColors {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback;
  return {
    person: token('--muted-foreground', '#9ba7c5'),
    personSelected: token('--primary', '#6886d5'),
    critical: token('--sev-critical', '#ff6b74'),
    caution: token('--sev-caution', '#f4b95f'),
    object: token('--point-soft', '#495e95'),
    camera: token('--faint', '#6b7797'),
  };
}

interface MockAppProps {
  /** Spatial REST Mock(MSW). 개발 서버에서 켰을 때만 있다. */
  mockApi?: SpatialMockApi | null;
  /** REST 기준 주소. MSW 가 켜져 있으면 '', 아니면 null(REST 를 쓰지 않음). */
  restBase?: string | null;
}

/**
 * 공간 상시 유지 + 하단 메뉴 + 업무 오버레이 목업과 Spatial 진단 도구.
 * ViewerShell 은 항상 한 번만 그린다. 개발 보기(devView)는 그 위 FE 목업·진단 레이어와 World 기준점의 표시만 바꾸고,
 * 업무 모드·선택·메뉴·패널·시점은 보기와 따로 보존한다.
 * 데이터 입력(source)은 보기와 따로 고르며, 바꿔도 Canvas·선택·모드는 그대로다.
 */
export function MockApp({ mockApi = null, restBase = null }: MockAppProps) {
  const viewer = useRef<Viewer3DHandle>(null);
  const [view, setView] = useState(() => readDevView(window.location.search));
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(() =>
    readDiagnosticsOpen(window.location.search),
  );
  const [menu, setMenu] = useState<MenuKey>('monitor');
  const [panelOpen, setPanelOpen] = useState(true);
  const [selected, setSelected] = useState<ViewerTarget | null>(null);
  const [status, setStatus] = useState<ViewerStatus | null>(null);
  const [light, setLight] = useState(false);
  const [colors, setColors] = useState(readColors);
  const { mockUi, spatial } = devViewOf(view);
  const [dataStore] = useState(createSpatialDataStore);
  const [sourceKey, setSourceKey] = useState(() => readDataSource(window.location.search));
  const [epoch, setEpoch] = useState(0);
  const [avatar] = useState(() => readAvatar(window.location.search));
  const [zoneApply, setZoneApply] = useState(() => readZoneApply(window.location.search));
  // 3D 편집 소비 예시(-195). 받은 초안·결과를 보여 주기만 한다.
  const [editPreview, setEditPreview] = useState<ViewerEditPreview | null>(null);
  const [editResult, setEditResult] = useState<ViewerEditResult | null>(null);
  const receiveEditResult = (result: ViewerEditResult) => {
    setEditResult(result);
    // 시작하지 못한 이유(REJECTED·다른 대상)는 진행 중인 초안을 지우지 않는다.
    if (result.status !== 'REJECTED' || result.target?.id === editPreview?.target.id) {
      setEditPreview(null);
    }
  };
  // 작업면 OBB 편집 소비 예시(-188). 구역과 같은 규칙으로 받은 값만 보인다.
  const [objectPreview, setObjectPreview] = useState<ViewerObjectEditPreview | null>(null);
  const [objectResult, setObjectResult] = useState<ViewerObjectEditResult | null>(null);
  const receiveObjectResult = (result: ViewerObjectEditResult) => {
    setObjectResult(result);
    if (result.status !== 'REJECTED' || result.target?.id === objectPreview?.target.id) {
      setObjectPreview(null);
    }
  };
  // 입력 없음이면 예전 고정 작업자·구역을, 입력이 있으면 Provider 의 사람·구역을 쓴다(둘을 함께 그리지 않는다).
  const legacy = sourceKey === 'off';
  const roster = useRoster(dataStore, legacy, status?.mode ?? 'LIVE');
  // 입력을 바꾸거나 처음부터 다시 재생할 때만 새 Source 를 만든다(epoch 는 재시작 표시).
  const harness = useMemo(
    () => createHarnessSource(sourceKey, restBase, mockApi),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sourceKey, restBase, mockApi, epoch],
  );

  const changeView = (next: DevViewKey) => {
    setView(next);
    // 페이지를 다시 불러오지 않고 주소만 바꾼다. 업무 모드는 건드리지 않는다.
    window.history.replaceState(window.history.state, '', withDevView(window.location.href, next));
  };

  const changeSource = (next: DataSourceKey) => {
    setSourceKey(next);
    window.history.replaceState(
      window.history.state,
      '',
      withDataSource(window.location.href, next),
    );
  };

  const changeZoneApply = (next: ZoneApplyState) => {
    setZoneApply(next);
    window.history.replaceState(
      window.history.state,
      '',
      withZoneApply(window.location.href, next),
    );
  };

  const selectMenu = (next: MenuKey) => {
    if (next === menu) {
      // 같은 메뉴는 패널만 열고 닫는다. 업무 모드는 그대로다.
      setPanelOpen((open) => !open);
      return;
    }
    setMenu(next);
    setPanelOpen(true);
    // 다른 업무 메뉴를 고른 명시적 동작만 모드를 바꾼다. TBM 계획만 PLANNED 이고 나머지는 LIVE 다.
    viewer.current?.setMode(next === 'tbm' ? 'PLANNED' : 'LIVE');
  };

  const toggleTheme = () => {
    const next = !light;
    document.documentElement.classList.toggle('dark', !next);
    setLight(next);
    setColors(readColors());
  };

  const commands = {
    select: (target: ViewerTarget) => viewer.current?.select(target),
    focus: (target: ViewerTarget) => viewer.current?.focus(target),
    clear: () => viewer.current?.clearSelection(),
  };

  return (
    <div className="smk-root" data-view={view} data-mock-ui={mockUi ? '' : undefined}>
      <div className="smk-layer" data-testid="spatial-layer">
        <ViewerShell
          ref={viewer}
          onSelectionChange={setSelected}
          onStatusChange={setStatus}
          onEditPreview={setEditPreview}
          onEditResult={receiveEditResult}
          onObjectEditPreview={setObjectPreview}
          onObjectEditResult={receiveObjectResult}
          source={harness.source}
          dataStore={dataStore}
          personModelUrl={avatar === 'xbot' ? DEV_XBOT_URL : null}
          zoneApplyState={zoneApply}
        >
          <MockScene colors={colors} selected={selected} legacy={legacy} />
          {spatial && <WorldReference />}
          <DebugProbe />
          <EditProbe />
        </ViewerShell>
      </div>

      {mockUi && (
        <div className="smk-ui" data-testid="mock-ui">
          <MockTopBar status={status} light={light} onToggleTheme={toggleTheme} store={dataStore} />
          <SelectionCard selected={selected} focus={commands.focus} clear={commands.clear} />
          {panelOpen && (
            <MockOverlay
              menu={menu}
              selected={selected}
              status={status}
              roster={roster}
              zoneApply={zoneApply}
              onClose={() => setPanelOpen(false)}
              {...commands}
            />
          )}
          <MockNav menu={menu} open={panelOpen} onSelect={selectMenu} />
        </div>
      )}

      {spatial && (
        <SpatialDiagnostics
          open={diagnosticsOpen}
          onToggle={() => setDiagnosticsOpen((open) => !open)}
          status={status}
          selected={selected}
        >
          <DataPanel
            store={dataStore}
            sourceKey={sourceKey}
            harness={harness}
            mockApi={mockApi}
            onSourceChange={changeSource}
            zoneApply={zoneApply}
            onZoneApplyChange={changeZoneApply}
            mode={status?.mode ?? 'LIVE'}
            onRestart={() => setEpoch((n) => n + 1)}
          />
          <EditPanel
            viewer={viewer}
            store={dataStore}
            selected={selected}
            preview={editPreview}
            result={editResult}
            objectPreview={objectPreview}
            objectResult={objectResult}
          />
        </SpatialDiagnostics>
      )}

      <DevViewSwitcher view={view} onChange={changeView} />
    </div>
  );
}
