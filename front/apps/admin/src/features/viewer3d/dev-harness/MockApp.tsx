import { DebugPanel, DebugProbe, readDebugFlag } from '@/features/viewer3d/dev-harness/MockDebug';
import { MockNav } from '@/features/viewer3d/dev-harness/MockNav';
import { MockOverlay, SelectionCard } from '@/features/viewer3d/dev-harness/MockOverlay';
import { MockScene, type SceneColors } from '@/features/viewer3d/dev-harness/MockScene';
import { MockTopBar } from '@/features/viewer3d/dev-harness/MockTopBar';
import { readMockUi, withMockUi, type MenuKey } from '@/features/viewer3d/dev-harness/mockUi';
import type { Viewer3DHandle, ViewerStatus, ViewerTarget } from '@/features/viewer3d/types';
import { ViewerShell } from '@/features/viewer3d/ViewerShell';
import { useEffect, useRef, useState } from 'react';

// 3D 재질 색도 FE 토큰(packages/ui globals.css)에서 읽어 다크·화이트 모드를 따른다.
function readColors(): SceneColors {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback;
  return {
    floor: token('--surface-2', '#19233b'),
    person: token('--muted-foreground', '#9ba7c5'),
    personSelected: token('--primary', '#6886d5'),
    critical: token('--sev-critical', '#ff6b74'),
    caution: token('--sev-caution', '#f4b95f'),
    object: token('--point-soft', '#495e95'),
    camera: token('--faint', '#6b7797'),
  };
}

/**
 * 공간 상시 유지 + 하단 메뉴 + 업무 오버레이 목업.
 * ViewerShell 은 항상 한 번만 그리고, 메뉴·패널·목업 ON/OFF 는 그 위 오버레이만 바꾼다.
 */
export function MockApp() {
  const viewer = useRef<Viewer3DHandle>(null);
  const [mockUi, setMockUi] = useState(() => readMockUi(window.location.search));
  const [menu, setMenu] = useState<MenuKey>('monitor');
  const [panelOpen, setPanelOpen] = useState(true);
  const [selected, setSelected] = useState<ViewerTarget | null>(null);
  const [status, setStatus] = useState<ViewerStatus | null>(null);
  const [light, setLight] = useState(false);
  const [colors, setColors] = useState(readColors);
  const [debug] = useState(() => readDebugFlag(window.location.search));

  // TBM 계획 화면만 PLANNED 로 보여준다. 기존 공개 명령(setMode)만 쓴다.
  useEffect(() => {
    viewer.current?.setMode(mockUi && panelOpen && menu === 'tbm' ? 'PLANNED' : 'LIVE');
  }, [mockUi, panelOpen, menu]);

  const toggleMockUi = () => {
    const next = !mockUi;
    setMockUi(next);
    // 페이지를 다시 불러오지 않고 주소만 바꾼다.
    window.history.replaceState(window.history.state, '', withMockUi(window.location.href, next));
  };

  const selectMenu = (next: MenuKey) => {
    if (next === menu) setPanelOpen((open) => !open);
    else {
      setMenu(next);
      setPanelOpen(true);
    }
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
    <div className="smk-root">
      <div className="smk-layer" data-testid="spatial-layer">
        <ViewerShell ref={viewer} onSelectionChange={setSelected} onStatusChange={setStatus}>
          <MockScene colors={colors} selected={selected} />
          {debug && <DebugProbe />}
        </ViewerShell>
      </div>

      {mockUi && (
        <div className="smk-ui" data-testid="mock-ui">
          <MockTopBar status={status} light={light} onToggleTheme={toggleTheme} />
          <SelectionCard selected={selected} focus={commands.focus} clear={commands.clear} />
          {panelOpen && (
            <MockOverlay
              menu={menu}
              selected={selected}
              status={status}
              onClose={() => setPanelOpen(false)}
              {...commands}
            />
          )}
          <MockNav menu={menu} open={panelOpen} onSelect={selectMenu} />
        </div>
      )}

      {debug && <DebugPanel />}

      <button type="button" aria-pressed={mockUi} onClick={toggleMockUi} className="smk-toggle">
        목업 UI {mockUi ? 'ON' : 'OFF'}
      </button>
    </div>
  );
}
