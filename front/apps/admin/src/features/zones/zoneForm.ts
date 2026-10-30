import type { ViewerEditBox } from '@/features/viewer3d';
import type { JudgeType, Severity, Zone, ZoneBody } from '@/features/zones/types';
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

/** 구역 이름 최대 글자 수(화면 제한). */
export const NAME_MAX = 40;

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

export interface ZoneFormErrors {
  name?: string;
  judgeType?: string;
  zoneType?: string;
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

type ZoneShape = Pick<ZoneBody, 'polygon_xy' | 'z_min_m' | 'z_max_m'>;

/** 고친 상자의 바닥 직사각형과 높이. */
const shapeOfBox = (box: ViewerEditBox): ZoneShape => ({
  polygon_xy: boxToPolygon(box),
  z_min_m: box.min[2],
  z_max_m: box.max[2],
});

/** 받은 구역의 다각형과 높이. 상자를 고치지 않았을 때 그대로 보낸다. */
const shapeOfZone = (zone: Zone): ZoneShape => ({
  polygon_xy: zone.polygon.map(([x, y]) => [x, y] as [number, number]),
  z_min_m: zone.zBounds[0],
  z_max_m: zone.zBounds[1],
});

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
    ...(box ? shapeOfBox(box) : shapeOfZone(zone)),
    severity: form.severity,
    has_guardrail: zone.hasGuardrail,
    requirements: zone.requirements && { ...zone.requirements },
  };
}

/** 새 구역 양식 검사. 이름·판정 유형은 저장과 같은 규칙이고, 유형은 골라야 한다. 상자는 숫자 칸이 미리 걸러 늘 높이가 있다. */
export function newZoneErrors(form: ZoneForm): ZoneFormErrors {
  const errors = formErrors(form, { saved: { ...form, name: '' }, hasHeight: true });
  if (form.zoneType === '') errors.zoneType = '유형을 고르세요.';
  return errors;
}

/** 새 구역(v1) 본문. */
export function newZoneBodyOf(form: ZoneForm, box: ViewerEditBox): ZoneBody {
  return {
    name: form.name.trim(),
    zone_kind: form.zoneType,
    eval_type: form.judgeType,
    ...shapeOfBox(box),
    severity: form.severity,
    has_guardrail: null,
    requirements: null,
  };
}
