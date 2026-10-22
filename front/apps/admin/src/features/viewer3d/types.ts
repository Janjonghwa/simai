import type { SpatialSource } from '@/features/viewer3d/data/spatialSource';
import type {
  ViewerEditCommandResult,
  ViewerEditPreview,
  ViewerEditResult,
} from '@/features/viewer3d/editing/editTypes';
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
  /**
   * 구역 형상 편집을 시작한다(S15P31A101-195, Spatial 선제안·FE 미합의). 그 구역을 선택하고 손잡이를 띄운다.
   * 원본은 바꾸지 않고 초안만 바꾼다. REPLAY 는 NOT_EDITABLE. 지금은 zone 만 받는다.
   */
  beginEdit(target: ViewerTarget): ViewerEditCommandResult;
  /** 초안을 버린다. 편집 중이 아니면 null. 결과는 onEditResult 로도 알린다. */
  cancelEdit(): ViewerEditResult | null;
  /** 초안을 World 결과로 돌려주고 편집을 끝낸다. 저장하지 않는다(saved: false). 편집 중이 아니면 null. */
  finishEdit(): ViewerEditResult | null;
}

export interface Viewer3DProps {
  ref?: Ref<Viewer3DHandle>;
  /** 처음 마운트할 때만 읽는다. 이후 바꾸려면 setMode 를 쓴다. */
  initialMode?: ViewerMode;
  /** 3D 클릭이든 FE 명령이든 선택이 바뀔 때마다 부른다. 해제는 null. */
  onSelectionChange?: (target: ViewerTarget | null) => void;
  /** 마운트 직후 한 번, 이후 단계·모드·오류가 바뀔 때마다 부른다. */
  onStatusChange?: (status: ViewerStatus) => void;
  /**
   * World·위험 이벤트·Health 입력(Mock 또는 외부 Live). 렌더마다 새로 만들지 말고 고정해 넘긴다.
   * 다른 Source(다른 id)로 바꾸면 받은 데이터만 비우고 Canvas·카메라·선택·모드는 그대로다. 없으면 데이터 없이 장면만 그린다.
   */
  source?: SpatialSource | null;
  /**
   * 편집 초안. 시작할 때와 손잡이를 놓을 때마다 부른다(끄는 도중에는 부르지 않는다). 저장 성공이 아니다.
   * 선택 사항이며 없으면 편집 결과를 받지 않는다(기존 화면 그대로).
   */
  onEditPreview?: (preview: ViewerEditPreview) => void;
  /** 편집이 끝날 때(완료·취소·거부) 부른다. 시작하지 못한 이유(REJECTED)도 알린다. */
  onEditResult?: (result: ViewerEditResult) => void;
  className?: string;
}
