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
import { useFloatSpot } from '@/hooks/useFloatSpot';
import { FLOAT_MARGIN } from '@/lib/floatSpot';
import { clearZoneDrafts, useHasZoneDrafts } from '@/store/zoneEdit.store';
import { startZoneEdit, stopZoneEdit } from '@/store/zoneEditMode.store';
import { Button } from '@simai/ui/components/button';
import { Popover, PopoverContent, PopoverTrigger } from '@simai/ui/components/popover';
import { TooltipProvider } from '@simai/ui/components/tooltip';
import { cn } from '@simai/ui/lib/utils';
import { useNavigate } from '@tanstack/react-router';
import { Video } from 'lucide-react';
import { useRef, useState } from 'react';

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

/**
 * 관리자 편집 모드 켜기·끄기. 켜져 있으면(editing) 아이콘이 X 로 바뀌고 누르면 끈다.
 * 저장 안 한 초안이 있으면 끌 때 버릴지 먼저 묻는다.
 */
function EditTool({ editing }: { editing: boolean }) {
  const hasDrafts = useHasZoneDrafts();
  const [confirming, setConfirming] = useState(false);
  const Icon = editing ? TOOL_ICON.zoneEditOff : TOOL_ICON.zoneEdit;
  const label = '관리자 편집 모드';

  // 이미 물어보는 중에 누르면 팝오버가 스스로 닫히게 두고 다시 열지 않는다.
  const toggle = () => {
    if (!editing) startZoneEdit();
    else if (!hasDrafts) stopZoneEdit();
    else if (!confirming) setConfirming(true);
  };
  const discard = () => {
    clearZoneDrafts();
    stopZoneEdit();
    setConfirming(false);
  };

  return (
    <Popover
      open={confirming}
      onOpenChange={(open) => {
        if (!open) setConfirming(false);
      }}
    >
      <ToolTooltip
        label={label}
        trigger={
          <PopoverTrigger
            render={
              <Button
                variant={editing ? 'secondary' : 'ghost'}
                size={TOOL_BUTTON_SIZE}
                aria-label={label}
                aria-pressed={editing}
                onClick={toggle}
              />
            }
          />
        }
      >
        <Icon aria-hidden className={TOOL_ICON_CLASS} />
      </ToolTooltip>
      <PopoverContent aria-label="편집 끄기">
        <Button size="sm" variant="destructive" onClick={discard}>
          수정 버리고 끄기
        </Button>
        <Button size="sm" variant="outline" onClick={() => setConfirming(false)}>
          계속 편집
        </Button>
      </PopoverContent>
    </Popover>
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
 * 실시간 관제 화면의 도구 모음 하나. 왼쪽 위에 가로로 놓이고, 도구 모양 손잡이를 누르면 접고 펼치며 끌면 옮긴다.
 * 시점 초기화(또는 3D 다시 시도) · 관리자 편집 모드 · 맵 재생성이 들어 있고, 버튼에 올리면 이름이 바로 뜬다.
 * 맵 재생성 도구는 접거나 편집 중이어도 떼지 않고 숨기기만 한다(진행 상태를 계속 받는다).
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
  const [open, setOpen] = useState(true);
  const palette = useRef<HTMLDivElement>(null);
  const spot = useFloatSpot(palette, 'top', FLOAT_MARGIN);
  const Tool = TOOL_ICON.tool;
  const label = open ? '도구 접기' : '도구 펼치기';

  return (
    <TooltipProvider>
      <div
        ref={palette}
        role="group"
        aria-label="도구"
        inert={standby}
        style={spot.style}
        className="absolute top-[72px] left-3.5 z-[6] flex items-center gap-1 rounded-[14px] bg-card/94 p-1 shadow-float backdrop-blur-[8px]"
      >
        <ToolTooltip
          label={label}
          trigger={
            <button
              type="button"
              aria-expanded={open}
              aria-label={label}
              {...spot.handlers}
              onClick={() => setOpen(!open)}
              className={cn(
                'inline-flex size-9 touch-none items-center justify-center rounded-lg text-muted-foreground select-none hover:bg-muted hover:text-foreground',
                spot.dragging ? 'cursor-grabbing' : 'cursor-grab',
              )}
            />
          }
        >
          <Tool aria-hidden className={TOOL_ICON_CLASS} />
        </ToolTooltip>
        {open && (
          <>
            <ViewTool view={view} onResetView={onResetView} onRetry3d={onRetry3d} />
            {onToggleCctv && (
              <ToolTooltip
                label="CCTV 창"
                trigger={
                  <Button
                    variant="ghost"
                    size={TOOL_BUTTON_SIZE}
                    aria-label="CCTV 창"
                    aria-pressed={cctvOpen}
                    onClick={onToggleCctv}
                  />
                }
              >
                <Video aria-hidden className={TOOL_ICON_CLASS} />
              </ToolTooltip>
            )}
            {isAdmin && <EditTool editing={editing} />}
            {editing && zonesCommitted && <TbmNavigateTool />}
          </>
        )}
        {isAdmin && (
          <SceneRegenerateTool
            hidden={!open || editing}
            currentSceneVersion={currentSceneVersion}
            currentScene={currentScene}
          />
        )}
      </div>
    </TooltipProvider>
  );
}
