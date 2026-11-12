import { importViewer } from '@/features/layout/spatialHost/useViewerMode';
import { useThemeStore } from '@/features/layout/theme.store';
import { useSceneView } from '@/features/layout/useSceneView';
import { ViewerLoadBoundary } from '@/features/monitor/ViewerLoadBoundary';
import {
  sessionSceneVersion,
  zoneMapTitle,
  type SceneTierStatusView,
} from '@/features/setup/setup';
import type { ViewerStatus } from '@/features/viewer3d';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { lazy, Suspense, useCallback, useState, type ReactNode } from 'react';

// 보정·생성 단계의 3D 미리보기. three.js 묶음은 그릴 때만 받는다(값은 동적 import).
const Viewer3D = lazy(importViewer);

/** 오른쪽(또는 한 열일 때 전체) 카드 열. 카드가 길면 이 열 안에서만 스크롤한다. */
export const CALIBRATE_COLUMN = 'flex min-h-0 min-w-0 flex-col gap-2.5 [&>*]:shrink-0';

/** 3D 칸이 없을 때(또는 맵이 준비되기 전) 카드 열 하나를 가운데에 두는 폭. */
export const NARROW_COLUMN = 'mx-auto w-full max-w-[560px]';

/** 1100px 이상에서 3D 가 왼쪽 1.7, 카드 열이 오른쪽 1. 그보다 좁으면 한 열로 쌓는다. */
const SPLIT =
  'grid min-h-[420px] flex-1 gap-3 min-[1100px]:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)] min-[1100px]:grid-rows-[minmax(0,1fr)]';

/**
 * 미리보기 맵이 준비된 뒤의 배치. 이 세션이 만든 Scene(정밀 3D 맵이 있으면 정밀, 아니면 미리보기)을 버전을 지정해
 * 받아(GET /api/scenes/{scene_version}/view, 지금 관제 Scene 과 무관) 받으면 왼쪽에 3D 를, 오른쪽에 카드 열(children)을 둔다.
 * 맵이 아직 없거나 Scene 을 받지 못했거나 3D 를 띄우지 못하면 3D 칸 없이 한 열이다.
 * 관제 틀(useViewportHost)은 쓰지 않는다: 전역 편집 상태를 건드리지 않고 Scene 만 그린다.
 */
export function SetupPreviewSplit({
  sceneTier,
  children,
}: {
  sceneTier: SceneTierStatusView;
  children: ReactNode;
}) {
  const theme = useThemeStore((s) => s.theme);
  const sceneVersion = sessionSceneVersion(sceneTier);
  const { scene, onSceneStatusChange } = useSceneView(sceneVersion);
  // 3D 를 띄우지 못한 Scene 버전. 다른 버전(정식 Scene 등)으로 바뀌면 다시 띄워 본다.
  const [failedVersion, setFailedVersion] = useState<string | null>(null);
  const onLoadError = useCallback(() => setFailedVersion(sceneVersion), [sceneVersion]);
  const onStatusChange = useCallback(
    (status: ViewerStatus) => {
      if (status.phase === 'error') setFailedVersion(sceneVersion);
    },
    [sceneVersion],
  );
  const hasModel = sceneVersion !== null && sceneVersion !== failedVersion && scene?.ok === true;

  return (
    <div className={hasModel ? SPLIT : NARROW_COLUMN}>
      {hasModel && (
        <Card size="sm" className="h-[420px] min-[1100px]:h-auto">
          <CardHeader>
            <CardTitle>
              <h2>{zoneMapTitle(sceneTier)}</h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="min-h-0 flex-1">
            <div
              data-testid="setup-preview-viewport"
              className="relative size-full overflow-hidden rounded-lg bg-background"
            >
              <ViewerLoadBoundary onError={onLoadError}>
                <Suspense fallback={null}>
                  <Viewer3D
                    theme={theme}
                    scene={scene}
                    onSceneStatusChange={onSceneStatusChange}
                    onStatusChange={onStatusChange}
                    className="absolute inset-0"
                  />
                </Suspense>
              </ViewerLoadBoundary>
            </div>
          </CardContent>
        </Card>
      )}
      <div className={cn(CALIBRATE_COLUMN, hasModel && 'min-[1100px]:overflow-y-auto')}>
        {children}
      </div>
    </div>
  );
}
