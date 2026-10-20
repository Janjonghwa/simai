import type { Ref } from 'react';

// FE 와 주고받는 값의 모양이다. FE 통합(S15P31A101-193 완료 조건) 때 함께 확정하며, 아직 팀 공통 계약이 아니다.

/** 관제 모드. LIVE 관측, PLANNED TBM 가상 배치, REPLAY 기록 재생(명세 13.4). */
export const VIEWER_MODES = ['LIVE', 'PLANNED', 'REPLAY'] as const;
export type ViewerMode = (typeof VIEWER_MODES)[number];

/** 3D 에서 고를 수 있는 대상. person 은 world_id, zone 은 zone_id, object 는 등록 OBB(작업면·정적 객체)의 ID 다. */
export const VIEWER_TARGET_TYPES = ['person', 'zone', 'object'] as const;
export type ViewerTargetType = (typeof VIEWER_TARGET_TYPES)[number];

export interface ViewerTarget {
  type: ViewerTargetType;
  id: string;
}

export type ViewerPhase = 'initializing' | 'ready' | 'error';

/** RENDER_FAILED: 렌더러를 만들지 못했거나 장면이 예외를 냈다. CONTEXT_LOST: 브라우저가 WebGL 컨텍스트를 회수했다. */
export type ViewerErrorCode = 'RENDER_FAILED' | 'CONTEXT_LOST';

export interface ViewerError {
  code: ViewerErrorCode;
  message: string;
}

export interface ViewerStatus {
  phase: ViewerPhase;
  mode: ViewerMode;
  error: ViewerError | null;
}

/** INVALID_*: 값의 모양이 틀렸다. NOT_FOUND: 장면에 없는 대상이다. NOT_READY: 렌더러가 준비되지 않았거나 오류 상태다. */
export type CommandFailureReason = 'INVALID_TARGET' | 'INVALID_MODE' | 'NOT_FOUND' | 'NOT_READY';

export type CommandResult = { ok: true } | { ok: false; reason: CommandFailureReason };

/** FE → 뷰어 명령. 실패해도 예외를 던지지 않고 이유를 돌려준다. */
export interface Viewer3DHandle {
  select(target: ViewerTarget): CommandResult;
  clearSelection(): CommandResult;
  focus(target: ViewerTarget): CommandResult;
  setMode(mode: ViewerMode): CommandResult;
}

export interface Viewer3DProps {
  ref?: Ref<Viewer3DHandle>;
  /** 처음 마운트할 때만 읽는다. 이후 바꾸려면 setMode 를 쓴다. */
  initialMode?: ViewerMode;
  /** 3D 클릭이든 FE 명령이든 선택이 바뀔 때마다 부른다. 해제는 null. */
  onSelectionChange?: (target: ViewerTarget | null) => void;
  /** 마운트 직후 한 번, 이후 단계·모드·오류가 바뀔 때마다 부른다. */
  onStatusChange?: (status: ViewerStatus) => void;
  className?: string;
}
