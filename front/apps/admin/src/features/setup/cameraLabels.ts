// 세팅 화면에 보이는 카메라 이름과 옆 카메라 쌍 판정.
// 서버 카메라 id(cam1..4, cam_A..D)는 화면에 쓰지 않고, id 목록 안의 순서로 이름을 만든다.

/** 카메라 id 목록 안의 순서(1부터). 목록에 없으면 null. */
export function cameraNumber(cameraIds: readonly string[], id: string): number | null {
  const index = cameraIds.indexOf(id);
  return index < 0 ? null : index + 1;
}

/** '카메라 N'. 목록에 없는 id 는 그대로 돌려준다. */
export function cameraLabel(cameraIds: readonly string[], id: string): string {
  const n = cameraNumber(cameraIds, id);
  return n === null ? id : `카메라 ${n}`;
}

/** '캠N'. 목록에 없는 id 는 그대로 돌려준다. */
export function cameraShortLabel(cameraIds: readonly string[], id: string): string {
  const n = cameraNumber(cameraIds, id);
  return n === null ? id : `캠${n}`;
}

/** '캠1–캠2'. */
export function pairLabel(cameraIds: readonly string[], a: string, b: string): string {
  return `${cameraShortLabel(cameraIds, a)}–${cameraShortLabel(cameraIds, b)}`;
}

/**
 * 보정이 쓰는 옆 카메라 쌍인지(목록 순서가 모서리를 도는 순서라 1–2, 2–3, 3–4, 4–1).
 * 마주 보는 쌍(1–3, 2–4)과 목록에 없는 id 는 아니다.
 */
export function isNeighborPair(cameraIds: readonly string[], a: string, b: string): boolean {
  const i = cameraIds.indexOf(a);
  const j = cameraIds.indexOf(b);
  if (i < 0 || j < 0 || i === j) return false;
  const gap = Math.abs(i - j);
  return gap === 1 || gap === cameraIds.length - 1;
}
