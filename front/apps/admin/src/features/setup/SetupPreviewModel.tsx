import { importViewer } from '@/features/layout/spatialHost/useViewerMode';
import { useThemeStore } from '@/features/layout/theme.store';
import { useSceneOrCurrent } from '@/features/layout/useSceneView';
import { ViewerLoadBoundary } from '@/features/monitor/ViewerLoadBoundary';
import {
  sessionSceneVersion,
  zoneMapTitle,
  type SceneTierStatusView,
} from '@/features/setup/setup';
import type { ViewerStatus } from '@/features/viewer3d';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { Loader2 } from 'lucide-react';
import { lazy, Suspense, useCallback, useState, type ReactNode } from 'react';

// 보정·생성 단계의 3D 미리보기. three.js 묶음은 그릴 때만 받는다(값은 동적 import).
const Viewer3D = lazy(importViewer);

/** 오른쪽(또는 한 열일 때 전체) 카드 열. 카드가 길면 이 열 안에서만 스크롤한다. */
export const CALIBRATE_COLUMN = 'flex min-h-0 min-w-0 flex-col gap-2.5 [&>*]:shrink-0';

/** 미리보기 맵이 준비되기 전에 카드 열 하나를 가운데에 두는 폭. */
export const NARROW_COLUMN = 'mx-auto w-full max-w-[560px]';

/** 1100px 이상에서 3D 가 왼쪽 1.7, 카드 열이 오른쪽 1. 그보다 좁으면 한 열로 쌓는다. */
const SPLIT =
  'grid min-h-[420px] flex-1 gap-3 min-[1100px]:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)] min-[1100px]:grid-rows-[minmax(0,1fr)]';

/**
 * 미리보기 맵이 준비된 뒤의 배치: 왼쪽에 3D 칸, 오른쪽에 카드 열(children). 3D 칸은 Scene 을 받기 전에도(404 로 다시 받는 중이어도)
 * 빈 틀로 자리를 지켜, 받는 순간 배치가 바뀌지 않는다. 받는 동안은 로딩 표시만 두고, 3D 를 띄우지 못하면 빈 틀만 남긴다.
 * 이 세션이 만든 Scene(정밀 3D 맵이 있으면 정밀, 아니면 미리보기)을 버전을 지정해 받는다(GET /api/scenes/{scene_version}/view).
 * 만든 맵이 없는 세션(이전 모델 쓰기)은 버전이 없어 지금 관제 Scene 으로 그린다. 구역 단계와 같다.
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
  const { scene, onSceneStatusChange } = useSceneOrCurrent(sceneVersion);
  // 3D 를 띄우지 못한 Scene 버전(null 은 지금 관제 Scene). 다른 버전(정식 Scene 등)으로 바뀌면 다시 띄워 본다.
  const [failedScene, setFailedScene] = useState<{ version: string | null } | null>(null);
  const failed = failedScene?.version === sceneVersion;
  const onLoadError = useCallback(() => setFailedScene({ version: sceneVersion }), [sceneVersion]);
  const onStatusChange = useCallback(
    (status: ViewerStatus) => {
      if (status.phase === 'error') setFailedScene({ version: sceneVersion });
    },
    [sceneVersion],
  );
  const hasModel = !failed && scene?.ok === true;
  const loading = !failed && !hasModel;

  return (
    <div className={SPLIT}>
      <Card size="sm" className="h-[420px] min-[1100px]:h-auto">
        <CardHeader>
          <CardTitle>
            <h2>{zoneMapTitle(sceneTier)}</h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="min-h-0 flex-1">
          <div
            data-testid="setup-preview-viewport"
            aria-busy={loading || undefined}
            className="relative grid size-full place-items-center overflow-hidden rounded-lg bg-background text-muted-foreground"
          >
            {hasModel ? (
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
            ) : (
              loading && <Loader2 aria-hidden className="size-6 animate-spin" />
            )}
          </div>
        </CardContent>
      </Card>
      <div className={cn(CALIBRATE_COLUMN, 'min-[1100px]:overflow-y-auto')}>{children}</div>
    </div>
  );
}
