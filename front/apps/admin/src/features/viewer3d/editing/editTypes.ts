import type { ViewerMode, ViewerTarget } from '@/features/viewer3d/types';

// 3D 편집(S15P31A101-195)이 바깥(FE)에 알리는 값. Spatial 이 먼저 낸 선택 접점이며 FE·BE 와 합의한 저장 계약이 아니다.
// - 미리보기·결과는 저장 성공을 뜻하지 않는다(saved: false 고정). 저장·구성 적용(ACK)은 FE -189·BE -120 이 한다.
// - 좌표는 World(m, 오른손, +Z 위)의 축 정렬 상자 min·max 다. 네 모서리·다각형 같은 정식 구역 형상 계약이 아니다.

/**
 * 편집을 시작하지 못했거나 중간에 끝난 이유.
 * INVALID_TARGET 대상 모양이 틀림 · NOT_EDITABLE 이 대상·모드(REPLAY)·조작은 편집할 수 없음 ·
 * NOT_READY 뷰어가 준비되지 않았거나 오류 · NOT_FOUND 장면에 그 구역이 없음 ·
 * STALE_SOURCE 편집 중 원본(구역 형상·입력 Source·Scene 버전)이 바뀜 · INVALID_GEOMETRY 결과 형상이 유효하지 않음.
 */
export const VIEWER_EDIT_REJECT_REASONS = [
  'INVALID_TARGET',
  'NOT_EDITABLE',
  'NOT_READY',
  'NOT_FOUND',
  'STALE_SOURCE',
  'INVALID_GEOMETRY',
] as const;
export type ViewerEditRejectReason = (typeof VIEWER_EDIT_REJECT_REASONS)[number];

/** 취소 이유. USER 는 cancelEdit·Esc, 나머지는 선택·모드가 바뀌었거나 다른 편집으로 바뀌었거나 뷰어가 내려간 경우다. */
export type ViewerEditCancelReason =
  'USER' | 'SELECTION_CHANGED' | 'MODE_CHANGED' | 'REPLACED' | 'UNMOUNTED';

/** World 축 정렬 상자(m, Z-up). */
export interface ViewerEditBox {
  min: [number, number, number];
  max: [number, number, number];
}

/** 편집을 시작할 때 실제로 확인한 출처. 모르는 값은 지어내지 않고 null 이다. */
export interface ViewerEditProvenance {
  /** 뷰어가 구독 중이던 입력 Source 의 id. */
  sourceId: string | null;
  /** 구역 형상 계약 표시. 지금은 개발용 DEMO 형상만 받는다. */
  zoneContract: 'DEMO_UNCONFIRMED';
  /** 구역 목록을 받은 시각(이 브라우저 시계, ms). */
  zonesReceivedAt: number | null;
  /** 편집을 시작할 때 받은 World 의 scene_version. World 를 받기 전이면 null. */
  sceneVersion: string | null;
  /** 구역 형상에는 구성 버전이 실려 오지 않아 늘 null 이다(BE -120 계약 전). */
  configVersion: null;
}

/** 편집 중 초안. 원본(before)은 바뀌지 않는다. */
export interface ViewerEditPreview {
  target: ViewerTarget;
  mode: ViewerMode;
  before: ViewerEditBox;
  draft: ViewerEditBox;
  provenance: ViewerEditProvenance;
  saved: false;
}

export type ViewerEditResult =
  | {
      status: 'FINISHED';
      target: ViewerTarget;
      mode: ViewerMode;
      before: ViewerEditBox;
      after: ViewerEditBox;
      /** 초안이 원본과 다른지. */
      changed: boolean;
      provenance: ViewerEditProvenance;
      /** 저장 요청용 결과일 뿐 저장·적용 성공이 아니다. */
      saved: false;
    }
  | {
      status: 'CANCELLED';
      target: ViewerTarget;
      mode: ViewerMode;
      before: ViewerEditBox;
      reason: ViewerEditCancelReason;
      provenance: ViewerEditProvenance;
      saved: false;
    }
  | {
      status: 'REJECTED';
      target: ViewerTarget | null;
      reason: ViewerEditRejectReason;
      detail: string;
      saved: false;
    };

export type ViewerEditCommandResult = { ok: true } | { ok: false; reason: ViewerEditRejectReason };
