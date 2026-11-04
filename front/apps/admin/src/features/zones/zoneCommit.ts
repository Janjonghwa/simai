import { bodyOf as surfaceBodyOf } from '@/features/surfaces/surfaces';
import type { WorkSurface } from '@/features/surfaces/types';
import type { ViewerEditBox } from '@/features/viewer3d';
import type { Zone, ZoneCommitRequest, ZoneCommitSurface } from '@/features/zones/types';
import type { SurfaceDraft } from '@/features/zones/zoneEditModel';
import { newZoneBodyOf, bodyOf as zoneBodyOf, type ZoneForm } from '@/features/zones/zoneForm';

// 구역 편집 저장 요청 본문(순수 계산). 구역·작업면 변경을 POST /api/zones/commit 요청 하나로 만든다. 보낼 것이 없으면 null 이다.
// 보내기·실패 처리는 useZoneCommitMutation 이 맡는다.

/** 작업면 초안의 이름을 서버 본문 쓰는 값으로. */
const nameOf = (draft: SurfaceDraft) => draft.name ?? '';

/**
 * 이은 작업면(linked)과 초안(draft)의 차이를 작업면 op 하나로. 건드리지 않았거나(undefined·changed 거짓) 바꿀 것이 없으면
 * undefined 다. 새 작업면이 이은 작업면을 대신하면(지우고 다시 추가) REPLACE 다(구역에는 작업면 하나).
 */
function surfacePartOf(
  linked: WorkSurface | null,
  draft: SurfaceDraft | null | undefined,
  changed: boolean,
): ZoneCommitSurface | undefined {
  if (draft === undefined || !changed) return undefined;
  if (draft === null) return linked ? { op: 'ARCHIVE', object_id: linked.objectId } : undefined;
  if (linked === null) {
    return { op: 'CREATE', body: surfaceBodyOf(null, nameOf(draft), draft.geometry) };
  }
  if (linked.objectId === draft.id) {
    // 상판 높이 표준편차는 받은 값을 다시 보낸다(linked 를 넘긴다).
    const body = surfaceBodyOf(linked, nameOf(draft), draft.geometry);
    return { op: 'UPDATE', object_id: linked.objectId, body };
  }
  const body = surfaceBodyOf(null, nameOf(draft), draft.geometry);
  return { op: 'REPLACE', object_id: linked.objectId, body };
}

export interface SaveRequestInput {
  zone: Zone;
  form: ZoneForm;
  /** 고친 구역 상자(고치지 않았으면 null). */
  box: ViewerEditBox | null;
  /** 구역 속성이나 상자를 고쳤는지. 작업면만 고쳤으면 구역은 KEEP 이라 새 버전을 만들지 않는다. */
  zoneChanged: boolean;
  linked: WorkSurface | null;
  surface: SurfaceDraft | null | undefined;
  surfaceChanged: boolean;
}

/** 기존 구역 저장(구역 새 버전·작업면 바꾸기). 바뀐 것이 없으면 null. */
export function saveRequestOf(input: SaveRequestInput): ZoneCommitRequest | null {
  const { zone, form, box, zoneChanged, linked, surface, surfaceChanged } = input;
  const surfacePart = surfacePartOf(linked, surface, surfaceChanged);
  if (!zoneChanged && !surfacePart) return null;
  const zonePart = zoneChanged
    ? ({ op: 'UPDATE', zone_id: zone.zoneId, body: zoneBodyOf(zone, form, box) } as const)
    : ({ op: 'KEEP', zone_id: zone.zoneId } as const);
  return surfacePart ? { zone: zonePart, surface: surfacePart } : { zone: zonePart };
}

/** 새 구역 만들기. 구역 몸체는 상자·양식 그대로다. 작업면 초안이 있으면 새 작업면도 함께 만든다. */
export function createRequestOf(input: {
  form: ZoneForm;
  box: ViewerEditBox;
  surface: SurfaceDraft | null | undefined;
}): ZoneCommitRequest {
  const zone = { op: 'CREATE', body: newZoneBodyOf(input.form, input.box) } as const;
  const surfacePart = surfacePartOf(null, input.surface, true);
  return surfacePart ? { zone, surface: surfacePart } : { zone };
}

/** 구역 삭제(보관). 이은 작업면은 서버가 함께 보관하므로 작업면 op 를 붙이지 않는다. */
export const archiveRequestOf = (zoneId: string): ZoneCommitRequest => ({
  zone: { op: 'ARCHIVE', zone_id: zoneId },
});
