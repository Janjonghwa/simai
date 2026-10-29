import type { ViewerEditBox, ViewerEditField, ViewerEditRejectReason } from '@/features/viewer3d';

export interface ZoneFieldSpec {
  field: ViewerEditField;
  label: string;
  shortLabel: string;
  unit: string;
  hint: string;
}

export const ZONE_FIELD_SPECS: readonly ZoneFieldSpec[] = [
  {
    field: 'originX',
    label: '시작 X (m)',
    shortLabel: '시작 X',
    unit: 'm',
    hint: '서쪽 기준점(min X) · 폭 유지 이동',
  },
  {
    field: 'originY',
    label: '시작 Y (m)',
    shortLabel: '시작 Y',
    unit: 'm',
    hint: '남쪽 기준점(min Y) · 깊이 유지 이동',
  },
  {
    field: 'width',
    label: '폭 X (m)',
    shortLabel: '폭 X',
    unit: 'm',
    hint: 'X 방향 길이(max X − min X) · 서쪽(X−) 고정',
  },
  {
    field: 'depth',
    label: '깊이 Y (m)',
    shortLabel: '깊이 Y',
    unit: 'm',
    hint: 'Y 방향 길이(max Y − min Y) · 남쪽(Y−) 고정',
  },
  {
    field: 'zMin',
    label: '하단 Z (m)',
    shortLabel: '하단 Z',
    unit: 'm',
    hint: '바닥·공중 하단 높이(zMin) · 상단 고정',
  },
  {
    field: 'zMax',
    label: '상단 Z (m)',
    shortLabel: '상단 Z',
    unit: 'm',
    hint: '상단 높이(zMax) · 하단 고정',
  },
] as const;

/** 타이핑 중 유효한 숫자를 로컬 3D 미리보기에 자동 반영하는 안정화 지연(ms). */
export const NUMERIC_DEBOUNCE_MS = 220;

export function formatMeters(value: number): string {
  return (Math.round(value * 1000) / 1000).toFixed(2);
}

export function formatFieldNumber(value: number): string {
  return String(Number(value.toFixed(4)));
}

export function readFieldValue(box: ViewerEditBox, field: ViewerEditField): number {
  switch (field) {
    case 'originX':
      return box.min[0];
    case 'originY':
      return box.min[1];
    case 'width':
      return box.max[0] - box.min[0];
    case 'depth':
      return box.max[1] - box.min[1];
    case 'zMin':
      return box.min[2];
    case 'zMax':
      return box.max[2];
  }
}

/**
 * 한 칸을 고친 상자. 뷰어 숫자 입력과 같은 규칙이다(시작 X·Y 는 크기를 두고 옮기고, 폭·깊이는 시작 쪽을, 하단·상단은 반대쪽을
 * 고정한다). 폭·깊이가 0 이하이거나 하단이 상단 이상이면(또는 하단이 바닥 아래면) null 이다.
 * viewer3d 의 fieldOperation 은 값으로 불러오면 3D 묶음(three.js)이 첫 화면에 실려, 3D 밖 새 구역 양식은 이것을 쓴다.
 */
export function writeFieldValue(
  box: ViewerEditBox,
  field: ViewerEditField,
  value: number,
): ViewerEditBox | null {
  const min: [number, number, number] = [box.min[0], box.min[1], box.min[2]];
  const max: [number, number, number] = [box.max[0], box.max[1], box.max[2]];
  switch (field) {
    case 'originX':
      max[0] = value + (box.max[0] - box.min[0]);
      min[0] = value;
      break;
    case 'originY':
      max[1] = value + (box.max[1] - box.min[1]);
      min[1] = value;
      break;
    case 'width':
      if (value <= 0) return null;
      max[0] = box.min[0] + value;
      break;
    case 'depth':
      if (value <= 0) return null;
      max[1] = box.min[1] + value;
      break;
    case 'zMin':
      if (value < 0 || value >= box.max[2]) return null;
      min[2] = value;
      break;
    case 'zMax':
      if (value <= box.min[2]) return null;
      max[2] = value;
      break;
  }
  return { min, max };
}

export function formatFieldDraft(box: ViewerEditBox): Record<ViewerEditField, string> {
  return {
    originX: formatFieldNumber(readFieldValue(box, 'originX')),
    originY: formatFieldNumber(readFieldValue(box, 'originY')),
    width: formatFieldNumber(readFieldValue(box, 'width')),
    depth: formatFieldNumber(readFieldValue(box, 'depth')),
    zMin: formatFieldNumber(readFieldValue(box, 'zMin')),
    zMax: formatFieldNumber(readFieldValue(box, 'zMax')),
  };
}

/** 입력 도중의 미완성 부호·소수점 문자열인지 판별한다(이 동안에는 이전 유효 초안을 그대로 유지한다). */
export function isIntermediateNumericInput(raw: string): boolean {
  const trimmed = raw.trim();
  return (
    trimmed === '' ||
    trimmed === '-' ||
    trimmed === '+' ||
    trimmed === '.' ||
    trimmed === '-.' ||
    trimmed === '+.' ||
    /^[+-]?[0-9]+\.$/.test(trimmed)
  );
}

export function parseFiniteNumber(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  const num = Number(trimmed);
  return Number.isFinite(num) ? num : null;
}

export function fieldErrorText(
  field: ViewerEditField,
  reason: ViewerEditRejectReason | 'NOT_NUMBER',
): string {
  if (reason === 'NOT_NUMBER') {
    return '유효한 숫자(m)를 입력하세요. 마지막 유효 미리보기를 유지합니다.';
  }
  if (reason === 'INVALID_GEOMETRY') {
    if (field === 'width' || field === 'depth') {
      return '폭·깊이는 0보다 커야 합니다. 마지막 유효 미리보기를 유지합니다.';
    }
    if (field === 'zMin' || field === 'zMax') {
      return '하단 Z(zMin)는 상단 Z(zMax)보다 작아야 합니다. 마지막 유효 미리보기를 유지합니다.';
    }
    return '경계가 교차하거나 크기가 0 이하입니다. 마지막 유효 미리보기를 유지합니다.';
  }
  return rejectReasonText(reason);
}

export function sameEditBox(a: ViewerEditBox, b: ViewerEditBox, eps = 1e-6): boolean {
  return (
    a.min.every((v, i) => Math.abs(v - b.min[i]!) <= eps) &&
    a.max.every((v, i) => Math.abs(v - b.max[i]!) <= eps)
  );
}

export interface FieldDeltaItem {
  field: ViewerEditField;
  shortLabel: string;
  beforeText: string;
  afterText: string;
  deltaText: string;
}

/** 원본 대비 실제로 바뀐 항목만 Δ와 이전값으로 추린다(동일 6개 수치 중복 노출 방지). */
export function changedFieldDeltas(
  before: ViewerEditBox,
  after: ViewerEditBox,
  eps = 1e-4,
): FieldDeltaItem[] {
  const items: FieldDeltaItem[] = [];
  for (const spec of ZONE_FIELD_SPECS) {
    const b = readFieldValue(before, spec.field);
    const a = readFieldValue(after, spec.field);
    const diff = a - b;
    if (Math.abs(diff) <= eps) continue;
    const sign = diff > 0 ? '+' : '';
    items.push({
      field: spec.field,
      shortLabel: spec.shortLabel,
      beforeText: formatMeters(b),
      afterText: formatMeters(a),
      deltaText: `${sign}${formatMeters(diff)}m`,
    });
  }
  return items;
}

/**
 * 보관된 미리보기 상자(after)로 세션을 다시 열 때, 중간 단계에서 zMin >= zMax 교차가 나지 않도록
 * 상단/하단 적용 순서를 정한다.
 */
export function continueOrder(
  base: ViewerEditBox,
  target: ViewerEditBox,
): Array<{ field: ViewerEditField; value: number }> {
  const zSteps: Array<{ field: ViewerEditField; value: number }> =
    target.min[2] >= base.max[2]
      ? [
          { field: 'zMax', value: target.max[2] },
          { field: 'zMin', value: target.min[2] },
        ]
      : [
          { field: 'zMin', value: target.min[2] },
          { field: 'zMax', value: target.max[2] },
        ];
  return [
    { field: 'originX', value: target.min[0] },
    { field: 'originY', value: target.min[1] },
    { field: 'width', value: target.max[0] - target.min[0] },
    { field: 'depth', value: target.max[1] - target.min[1] },
    ...zSteps,
  ];
}

export function rejectReasonText(reason: ViewerEditRejectReason, detail?: string): string {
  switch (reason) {
    case 'INVALID_GEOMETRY':
      return '경계가 교차하거나 크기(폭·깊이·높이)가 0 이하입니다. 마지막 유효 미리보기를 유지합니다.';
    case 'NOT_READY':
      return '손잡이를 끄는 중이거나 3D 뷰어가 아직 준비되지 않았습니다.';
    case 'NOT_EDITABLE':
      return detail ?? '현재 모드(REPLAY 등)나 대상은 편집할 수 없습니다.';
    case 'NOT_FOUND':
      return '3D 장면에 해당 구역이 아직 올라오지 않았습니다.';
    case 'STALE_SOURCE':
      return '원본 구역·Source·Scene 버전이 바뀌어 이전 편집을 닫았습니다.';
    case 'INVALID_TARGET':
      return '유효한 구역 대상이 아닙니다.';
  }
}
