import type { ViewerEditBox } from '@/features/viewer3d';
import type { JudgeType, Severity, Zone, ZoneBody, ZoneBox } from '@/features/zones/types';
import { boxToPolygon } from '@/features/zones/zones';

/**
 * 구역 속성 입력값(명세 FR-15). 유효 시작·종료는 서버에 칸이 없어(valid_from·valid_to 는 버전 시각) 아직 받지 않는다.
 * 요구 조건(requirements)은 확장이라 입력하지 않는다.
 */
export interface ZoneForm {
  name: string;
  judgeType: JudgeType;
  /** 서버 zone_kind. 비울 수 없다. */
  zoneType: string;
  severity: Severity;
}

export const NAME_MAX = 40;

export const JUDGE_TYPE_LABEL: Record<JudgeType, string> = {
  VOLUME_3D: '입체 (VOLUME_3D)',
  GROUND_FOOTPRINT: '바닥 (GROUND_FOOTPRINT)',
};

/** 계약 설명에 있는 유형 예. */
export const ZONE_TYPE_LABEL: Record<string, string> = {
  OPENING: '개구부',
  EQUIPMENT_PARKING: '장비 주차',
  LIFTING: '양중 하부',
};

export function formOf(zone: Zone): ZoneForm {
  return {
    name: zone.name,
    judgeType: zone.judgeType,
    zoneType: zone.zoneType,
    severity: zone.severity,
  };
}

export function sameForm(a: ZoneForm, b: ZoneForm): boolean {
  return (
    a.name.trim() === b.name.trim() &&
    a.judgeType === b.judgeType &&
    a.zoneType === b.zoneType &&
    a.severity === b.severity
  );
}

/** 사용자가 고친 칸만. 나머지 칸은 늘 서버의 최신 버전을 보인다(다시 받은 다른 사람의 변경을 덮지 않는다). */
export type ZonePatch = Partial<ZoneForm>;

/** saved 와 다른 칸만 남긴다. */
export function patchOf(saved: ZoneForm, next: ZoneForm): ZonePatch {
  const patch: ZonePatch = {};
  for (const key of Object.keys(next) as (keyof ZoneForm)[]) {
    if (next[key] !== saved[key]) Object.assign(patch, { [key]: next[key] });
  }
  return patch;
}

export interface ZoneFormErrors {
  name?: string;
  judgeType?: string;
}

/** saved 는 받은 구역의 값, hasHeight 는 높이(z_min_m·z_max_m)가 있는지다. */
export function formErrors(
  form: ZoneForm,
  { saved, hasHeight }: { saved: ZoneForm; hasHeight: boolean },
): ZoneFormErrors {
  const errors: ZoneFormErrors = {};
  const name = form.name.trim();
  if (name === '') errors.name = '이름을 입력하세요.';
  // 길이 제한은 서버에 없는 화면 제한이라 새로 고친 이름에만 둔다(받은 긴 이름이 다른 칸 저장을 막지 않는다).
  else if (name !== saved.name.trim() && name.length > NAME_MAX)
    errors.name = `이름은 ${NAME_MAX}자까지입니다.`;
  // 서버(INVALID_Z_RANGE)·계약: VOLUME_3D 는 높이가 있어야 한다.
  if (form.judgeType === 'VOLUME_3D' && !hasHeight)
    errors.judgeType = '높이가 없는 구역은 입체로 판정할 수 없습니다.';
  return errors;
}

/**
 * 새 버전 본문. 서버는 기록을 통째로 바꾸므로 화면에서 고치지 않는 칸(has_guardrail·requirements)도 받은 값을 그대로 보낸다.
 * 상자(box)를 고쳤으면 그 바닥 직사각형과 높이를, 아니면 받은 다각형·높이를 그대로 보낸다(속성만 고쳐도 직사각형이 아닌
 * 다각형을 상자로 덮지 않고, 한쪽만 있는 높이도 지우지 않는다). 이름을 고치지 않았으면 받은 이름을 그대로 보낸다.
 */
export function bodyOf(zone: Zone, form: ZoneForm, box: ViewerEditBox | null): ZoneBody {
  return {
    name: form.name === zone.name ? zone.name : form.name.trim(),
    zone_kind: form.zoneType,
    eval_type: form.judgeType,
    polygon_xy: box ? boxToPolygon(box) : zone.polygon.map(([x, y]) => [x, y] as [number, number]),
    z_min_m: box ? box.min[2] : zone.zBounds[0],
    z_max_m: box ? box.max[2] : zone.zBounds[1],
    severity: form.severity,
    has_guardrail: zone.hasGuardrail,
    requirements: zone.requirements && { ...zone.requirements },
  };
}

/** 새 구역의 바닥 크기(m)와 높이(m). 만든 뒤 바로 형상을 고친다. */
const NEW_ZONE_SIZE_M = 2;
const NEW_ZONE_HEIGHT_M = 2;

/**
 * 새 구역(v1) 본문. 기존 구역들을 감싸는 상자의 가운데 바닥에 둔다. 구역이 없으면 World 원점이다. 유형은 비울 수 없고
 * Tier 0 판정이 유형을 보므로(개구부는 가장자리 규칙) 관리자가 고른 것을 쓴다.
 */
export function newZoneBody(zones: readonly ZoneBox[], zoneType: string): ZoneBody {
  const center = (axis: 0 | 1) =>
    zones.length === 0
      ? 0
      : (Math.min(...zones.map((z) => z.min[axis])) + Math.max(...zones.map((z) => z.max[axis]))) /
        2;
  const half = NEW_ZONE_SIZE_M / 2;
  const [cx, cy] = [center(0), center(1)];
  return {
    name: '새 구역',
    zone_kind: zoneType,
    eval_type: 'VOLUME_3D',
    polygon_xy: boxToPolygon({ min: [cx - half, cy - half, 0], max: [cx + half, cy + half, 0] }),
    z_min_m: 0,
    z_max_m: NEW_ZONE_HEIGHT_M,
    severity: 'HIGH',
    has_guardrail: null,
    requirements: null,
  };
}
