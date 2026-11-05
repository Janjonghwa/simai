import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { SceneRegenerateTool } from '@/features/layout/spatialHost/SceneRegenerateTool';
import {
  TOOL_BUTTON_SIZE,
  TOOL_ICON,
  TOOL_ICON_CLASS,
} from '@/features/layout/spatialHost/toolIcons';
import { ToolTooltip } from '@/features/layout/spatialHost/ToolTooltip';
import type { SpatialViewMode } from '@/features/layout/spatialHost/useViewerMode';
import { useMonitorStandby } from '@/features/monitor/useTodayWorks';
import { useZoneEditActive } from '@/features/monitor/useZoneEditActive';
import { useSetupFlowStore } from '@/features/setup/setup.store';
import { Button } from '@simai/ui/components/button';
import { TooltipProvider } from '@simai/ui/components/tooltip';
import { useNavigate } from '@tanstack/react-router';

interface ToolPaletteProps {
  view: SpatialViewMode;
  onResetView: () => void;
  onRetry3d: () => void;
  currentSceneVersion: string | null;
  /** 현재 Scene 조회 응답 본문(맵 재생성 품질 표시용). */
  currentScene?: unknown;
  onToggleCctv?: () => void;
  cctvOpen?: boolean;
}

/** 3D 는 처음 시점으로 돌리고, 3D 가 멈춰 2D 를 보고 있으면 3D 를 다시 띄운다(관제 데모와 같다). */
function ViewTool({
  view,
  onResetView,
  onRetry3d,
}: Pick<ToolPaletteProps, 'view' | 'onResetView' | 'onRetry3d'>) {
  const is3d = view === '3d';
  const Icon = is3d ? TOOL_ICON.resetView : TOOL_ICON.retry3d;
  const label = is3d ? '시점 초기화' : '3D 다시 시도';
  return (
    <ToolTooltip
      label={label}
      trigger={
        <Button
          variant="ghost"
          size={TOOL_BUTTON_SIZE}
          aria-label={label}
          onClick={is3d ? onResetView : onRetry3d}
        />
      }
    >
      <Icon aria-hidden className={TOOL_ICON_CLASS} />
    </ToolTooltip>
  );
}

/** 떠 있는 CCTV 창을 열고 닫는다(S15P31A101-495). 창이 열려 있으면 눌린 모양이다. */
function CctvTool({ open, onToggle }: { open?: boolean; onToggle: () => void }) {
  const Icon = TOOL_ICON.cctv;
  return (
    <ToolTooltip
      label="CCTV 창"
      trigger={
        <Button
          variant="ghost"
          size={TOOL_BUTTON_SIZE}
          aria-label="CCTV 창"
          aria-pressed={open}
          onClick={onToggle}
        />
      }
    >
      <Icon aria-hidden className={TOOL_ICON_CLASS} />
    </ToolTooltip>
  );
}

/** 세팅에서 구역을 발행한 뒤 편집 중이면, 이어서 작업 관리로 간다(S15P31A101-468). */
function TbmNavigateTool() {
  const navigate = useNavigate();
  const workUnitId = useSetupFlowStore((s) => s.workUnitId);
  return (
    <Button
      size="lg"
      variant="secondary"
      onClick={() =>
        void navigate({
          to: '/work',
          search: workUnitId === null ? {} : { workUnitId: String(workUnitId) },
        })
      }
    >
      작업 관리(TBM)로 이동
    </Button>
  );
}

/**
 * 실시간 관제 화면의 도구 모음. 왼쪽 위에 고정된 아이콘 줄이고, 구역 목록·작업 목록 칼럼이 그 아래에 이어진다.
 * 시점 초기화(또는 3D 다시 시도) · CCTV 창 · 맵 재생성(관리자)이 들어 있고, 버튼에 올리면 이름이 바로 뜬다.
 * 세팅에서 구역을 발행한 뒤 편집 중이면 작업 관리로 가는 버튼이 줄 끝에 붙는다.
 * 구역 편집은 구역 목록에서 들어가고 구역 편집 패널에서 끝낸다. 맵 재생성 도구는 편집 중에도 떼지 않고 숨기기만 한다
 * (진행 상태를 계속 받는다).
 */
export function ToolPalette({
  view,
  onResetView,
  onRetry3d,
  currentSceneVersion,
  currentScene,
  onToggleCctv,
  cctvOpen,
}: ToolPaletteProps) {
  const isAdmin = useCurrentRole() === 'ADMIN';
  const editing = useZoneEditActive();
  // 대기 화면이 위를 덮는 동안은 눌러도 Tab 으로도 닿지 않게 한다.
  const standby = useMonitorStandby();
  const zonesCommitted = useSetupFlowStore((s) => s.zonesCommitted);

  return (
    <TooltipProvider>
      <div
        role="group"
        aria-label="도구"
        inert={standby}
        className="absolute top-[72px] left-3.5 z-[6] flex items-center gap-1 rounded-[14px] bg-card/94 p-1 shadow-float backdrop-blur-[8px]"
      >
        <ViewTool view={view} onResetView={onResetView} onRetry3d={onRetry3d} />
        {onToggleCctv && <CctvTool open={cctvOpen} onToggle={onToggleCctv} />}
        {isAdmin && (
          <SceneRegenerateTool
            hidden={editing}
            currentSceneVersion={currentSceneVersion}
            currentScene={currentScene}
          />
        )}
        {editing && zonesCommitted && <TbmNavigateTool />}
      </div>
    </TooltipProvider>
  );
}
